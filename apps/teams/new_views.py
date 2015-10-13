# Amara, universalsubtitles.org
#
# Copyright (C) 2013 Participatory Culture Foundation
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see
# http://www.gnu.org/licenses/agpl-3.0.html.

"""new_views -- New team views

This module holds view functions for new-style teams.  Eventually it should
replace the old views.py module.
"""

from __future__ import absolute_import
import functools
import json
import logging
import pickle

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.cache import cache
from django.core.paginator import Paginator
from django.core.urlresolvers import reverse
from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render, redirect, get_object_or_404
from django.utils.translation import ugettext as _

from . import views as old_views
from . import forms
from . import permissions
from . import tasks
from .behaviors import get_main_project
from .exceptions import ApplicationInvalidException
from .models import (Invite, Setting, Team, Project, TeamVideo,
                     TeamLanguagePreference, TeamMember, Application)
from .statistics import compute_statistics
from auth.models import CustomUser as User
from messages import tasks as messages_tasks
from subtitles.models import SubtitleLanguage
from teams.workflows import TeamWorkflow
from utils.breadcrumbs import BreadCrumb
from utils.pagination import AmaraPaginator
from utils.forms import autocomplete_user_view
from utils.text import fmt
from utils.translation import get_language_choices, get_language_label
from videos.models import Action, Video

logger = logging.getLogger('teams.views')

ACTIONS_PER_PAGE = 20
VIDEOS_PER_PAGE = 8
MEMBERS_PER_PAGE = 10

def team_view(view_func):
    @functools.wraps(view_func)
    def wrapper(request, slug, *args, **kwargs):
        if not request.user.is_authenticated():
            return redirect_to_login(request.path)
        try:
            team = Team.objects.get(slug=slug,
                                    members__user_id=request.user.id)
        except Team.DoesNotExist:
            raise Http404
        return view_func(request, team, *args, **kwargs)
    return wrapper

def admin_only_view(view_func):
    @functools.wraps(view_func)
    @team_view
    def wrapper(request, team, *args, **kwargs):
        member = team.get_member(request.user)
        if not member.is_admin():
            messages.error(request,
                           _("You are not authorized to see this page"))
            return redirect(team)
        return view_func(request, team, member, *args, **kwargs)
    return wrapper

def public_team_view(view_func):
    def wrapper(request, slug, *args, **kwargs):
        try:
            team = Team.objects.get(slug=slug)
        except Team.DoesNotExist:
            raise Http404
        return view_func(request, team, *args, **kwargs)
    return wrapper

def team_settings_view(view_func):
    """Decorator for the team settings pages."""
    @functools.wraps(view_func)
    def wrapper(request, slug, *args, **kwargs):
        team = get_object_or_404(Team, slug=slug)
        if not permissions.can_view_settings_tab(team, request.user):
            messages.error(request,
                           _(u'You do not have permission to edit this team.'))
            return HttpResponseRedirect(team.get_absolute_url())
        return view_func(request, team, *args, **kwargs)
    return login_required(wrapper)

def fetch_actions_for_activity_page(team, tab, page, params):
    if tab == 'team':
        action_qs = Action.objects.filter(team=team)
    else:
        video_language = params.get('video_language')
        subtitles_language = params.get('subtitles_language', 'any')
        if video_language == 'any':
            video_language = None
        action_qs = team.fetch_video_actions(video_language)
        if subtitles_language != 'any':
            action_qs = action_qs.filter(
                new_language__language_code=subtitles_language)

    end = page * ACTIONS_PER_PAGE
    start = end - ACTIONS_PER_PAGE

    if params.get('action_type', 'any') != 'any':
        action_qs = action_qs.filter(action_type=params.get('action_type'))

    action_qs = action_qs.select_related('new_language', 'video')

    sort = params.get('sort', '-created')
    action_qs = action_qs.order_by(sort)

    action_qs = action_qs[start:end].select_related(
        'user', 'new_language__video'
    )
    return list(action_qs)

@public_team_view
def videos(request, team):
    if team.is_old_style():
        return old_views.detail(request, team)

    if not team.user_is_member(request.user):
        raise Http404

    filters_form = forms.VideoFiltersForm(team, request.GET)
    if filters_form.is_bound and filters_form.is_valid():
        team_videos = filters_form.get_queryset()
    else:
        team_videos = (team.teamvideo_set.all()
                       .order_by('-created')
                       .select_related('video'))
        main_project = get_main_project(team)
        if main_project:
            team_videos = team_videos.filter(
                video__teamvideo__project=main_project)

    # We embed several modal forms on the page, but luckily we can use the
    # same code to handle them all
    form_classes = {
        'add': forms.NewAddTeamVideoForm,
        'edit': forms.NewEditTeamVideoForm,
        'bulk_edit': forms.BulkEditTeamVideosForm,
        'move': forms.MoveTeamVideosForm,
        'remove': forms.RemoveTeamVideosForm,
    }
    page_forms = {}
    for name, klass in form_classes.items():
        auto_id = '{}_id-%s'.format(name)
        if request.method == 'POST' and request.POST.get('form') == name:
            form = klass(team, request.user, auto_id=auto_id,
                         data=request.POST, files=request.FILES)
            if form.is_valid():
                if isinstance(form, forms.BulkTeamVideoForm):
                    form.save(qs=team_videos)
                else:
                    form.save()
                messages.success(request, form.message())
                return HttpResponseRedirect(request.build_absolute_uri())
        else:
            form = klass(team, request.user, auto_id=auto_id)
        page_forms[name] = form

    if filters_form.selected_project:
        # use the selected project by default on the add video form
        page_forms['add'].initial = {
            'project': filters_form.selected_project.id,
        }

    paginator = AmaraPaginator(team_videos, VIDEOS_PER_PAGE)
    page = paginator.get_page(request)

    if filters_form.is_bound and filters_form.is_valid():
        # Hack to convert the search index results to regular Video objects.
        # We will probably be able to drop this when we implement #838
        team_video_order = {
            result.team_video_pk: i
            for i, result in enumerate(page)
        }
        team_videos = list(
            TeamVideo.objects
            .filter(id__in=team_video_order.keys())
            .select_related('video')
        )
        team_videos.sort(key=lambda tv: team_video_order[tv.id])
    else:
        team_videos = list(page)

    return render(request, 'new-teams/videos.html', {
        'team': team,
        'team_videos': team_videos,
        'page': page,
        'paginator': paginator,
        'filters_form': filters_form,
        'forms': page_forms,
        'bulk_mode_enabled': team_videos and (
            page_forms['move'].enabled or
            page_forms['remove'].enabled or
            page_forms['bulk_edit'].enabled
        ),
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Videos')),
        ],
    })

@team_view
def members(request, team):
    if team.is_old_style():
        return old_views.detail_members(request, team)

    member = team.get_member(request.user)

    filters_form = forms.MemberFiltersForm(request.GET)

    if request.method == 'POST':
        edit_form = forms.EditMembershipForm(member, request.POST)
        if edit_form.is_valid():
            edit_form.save()
            return HttpResponseRedirect(request.path)
        else:
            logger.warning("Error updating team memership: %s (%s)",
                           edit_form.errors.as_text(),
                           request.POST)
            messages.warning(request, _(u'Error updating membership'))
    else:
        edit_form = forms.EditMembershipForm(member)

    members = filters_form.update_qs(
        team.members.select_related('user')
        .prefetch_related('user__userlanguage_set',
                          'projects_managed',
                          'languages_managed'))

    paginator = AmaraPaginator(members, MEMBERS_PER_PAGE)
    page = paginator.get_page(request)

    return render(request, 'new-teams/members.html', {
        'team': team,
        'page': page,
        'filters_form': filters_form,
        'edit_form': edit_form,
        'show_invite_link': permissions.can_invite(team, request.user),
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Members')),
        ],
    })

@team_view
def project(request, team, project_slug):
    project = get_object_or_404(team.project_set, slug=project_slug)
    if permissions.can_change_project_managers(team, request.user):
        form = request.POST.get('form')
        if request.method == 'POST' and form == 'add':
            add_manager_form = forms.AddProjectManagerForm(
                team, project, data=request.POST)
            if add_manager_form.is_valid():
                add_manager_form.save()
                member = add_manager_form.cleaned_data['member']
                msg = fmt(_(u'%(user)s added as a manager'), user=member.user)
                messages.success(request, msg)
                return redirect('teams:project', team.slug, project.slug)
        else:
            add_manager_form = forms.AddProjectManagerForm(team, project)

        if request.method == 'POST' and form == 'remove':
            remove_manager_form = forms.RemoveProjectManagerForm(
                team, project, data=request.POST)
            if remove_manager_form.is_valid():
                remove_manager_form.save()
                member = remove_manager_form.cleaned_data['member']
                msg = fmt(_(u'%(user)s removed as a manager'),
                          user=member.user)
                messages.success(request, msg)
                return redirect('teams:project', team.slug, project.slug)
        else:
            remove_manager_form = forms.RemoveProjectManagerForm(team, project)
    else:
        add_manager_form = None
        remove_manager_form = None

    data = {
        'team': team,
        'project': project,
        'managers': project.managers.all(),
        'add_manager_form': add_manager_form,
        'remove_manager_form': remove_manager_form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(project),
        ],
    }
    return team.new_workflow.render_project_page(request, team, project, data)

@team_view
def all_languages_page(request, team):
    video_language_counts = dict(team.get_video_language_counts())
    completed_language_counts = dict(team.get_completed_language_counts())

    all_languages = set(video_language_counts.keys() +
                        completed_language_counts.keys())
    languages = [
        (lc,
         get_language_label(lc),
         video_language_counts.get(lc, 0),
         completed_language_counts.get(lc, 0),
        )
        for lc in all_languages
        if lc != ''
    ]
    languages.sort(key=lambda row: (-row[2], row[1]))

    data = {
        'team': team,
        'languages': languages,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Languages')),
        ],
    }
    return team.new_workflow.render_all_languages_page(
        request, team, data,
    )

@team_view
def language_page(request, team, language_code):
    try:
        language_label = get_language_label(language_code)
    except KeyError:
        raise Http404
    if permissions.can_change_language_managers(team, request.user):
        form = request.POST.get('form')
        if request.method == 'POST' and form == 'add':
            add_manager_form = forms.AddLanguageManagerForm(
                team, language_code, data=request.POST)
            if add_manager_form.is_valid():
                add_manager_form.save()
                member = add_manager_form.cleaned_data['member']
                msg = fmt(_(u'%(user)s added as a manager'), user=member.user)
                messages.success(request, msg)
                return redirect('teams:language-page', team.slug,
                                language_code)
        else:
            add_manager_form = forms.AddLanguageManagerForm(team,
                                                            language_code)

        if request.method == 'POST' and form == 'remove':
            remove_manager_form = forms.RemoveLanguageManagerForm(
                team, language_code, data=request.POST)
            if remove_manager_form.is_valid():
                remove_manager_form.save()
                member = remove_manager_form.cleaned_data['member']
                msg = fmt(_(u'%(user)s removed as a manager'),
                          user=member.user)
                messages.success(request, msg)
                return redirect('teams:language-page', team.slug,
                                language_code)
        else:
            remove_manager_form = forms.RemoveLanguageManagerForm(
                team, language_code)
    else:
        add_manager_form = None
        remove_manager_form = None

    data = {
        'team': team,
        'language_code': language_code,
        'language': language_label,
        'managers': (team.members
                     .filter(languages_managed__code=language_code)),
        'add_manager_form': add_manager_form,
        'remove_manager_form': remove_manager_form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Languages'), 'teams:all-languages-page', team.slug),
            BreadCrumb(language_label),
        ],
    }
    return team.new_workflow.render_language_page(
        request, team, language_code, data,
    )

@team_view
def invite(request, team):
    if not permissions.can_invite(team, request.user):
        return HttpResponseForbidden(_(u'You cannot invite people to this team.'))
    if request.POST:
        form = forms.InviteForm(team, request.user, request.POST)
        if form.is_valid():
            # the form will fire the notifications for invitees
            # this cannot be done on model signal, since you might be
            # sending invites twice for the same user, and that borks
            # the naive signal for only created invitations
            form.save()
            return HttpResponseRedirect(reverse('teams:members',
                                                args=[team.slug]))
    else:
        form = forms.InviteForm(team, request.user)

    if team.is_old_style():
        template_name = 'teams/invite_members.html'
    else:
        template_name = 'new-teams/invite.html'

    return render(request, template_name,  {
        'team': team,
        'form': form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Members'), 'teams:members', team.slug),
            BreadCrumb(_('Invite')),
        ],
    })

@team_view
def autocomplete_invite_user(request, team):
    return autocomplete_user_view(request, team.invitable_users())

@team_view
def autocomplete_project_manager(request, team, project_slug):
    project = get_object_or_404(team.project_set, slug=project_slug)
    return autocomplete_user_view(request, project.potential_managers())

@team_view
def autocomplete_language_manager(request, team, language_code):
    return autocomplete_user_view(
        request,
        team.potential_language_managers(language_code))

def member_search(request, team, qs):
    query = request.GET.get('query')
    if query:
        members_qs = (qs.filter(user__username__icontains=query)
                      .select_related('user'))
    else:
        members_qs = TeamMember.objects.none()

    data = [
        {
            'value': member.user.username,
            'label': fmt(_('%(username)s (%(full_name)s)'),
                         username=member.user.username,
                         full_name=unicode(member.user)),
        }
        for member in members_qs
    ]

    return HttpResponse(json.dumps(data), mimetype='application/json')

@public_team_view
@login_required
def join(request, team):
    user = request.user

    if team.user_is_member(request.user):
        messages.info(request,
                      fmt(_(u'You are already a member of %(team)s.'),
                          team=team))
    elif team.is_open():
        member = TeamMember.objects.create(team=team, user=request.user,
                                           role=TeamMember.ROLE_CONTRIBUTOR)
        messages.success(request,
                         fmt(_(u'You are now a member of %(team)s.'),
                             team=team))
        messages_tasks.team_member_new.delay(member.pk)
    elif team.is_by_application():
        return application_form(request, team)
    else:
        messages.error(request,
                       fmt(_(u'You cannot join %(team)s.'), team=team))
    return redirect(team)

def application_form(request, team):
    try:
        application = team.applications.get(user=request.user)
    except Application.DoesNotExist:
        application = Application(team=team, user=request.user)
    try:
        application.check_can_submit()
    except ApplicationInvalidException, e:
        messages.error(request, e.message)
        return redirect(team)

    if request.method == 'POST':
        form = forms.ApplicationForm(application, data=request.POST)
        if form.is_valid():
            form.save()
            return redirect(team)
    else:
        form = forms.ApplicationForm(application)
    return render(request, "new-teams/application.html", {
        'team': team,
        'form': form,
    })

@public_team_view
def admin_list(request, team):
    if team.is_old_style():
        return old_views.detail_members(request, team,
                                        role=TeamMember.ROLE_ADMIN)

    # The only real reason to view this page is if you want to ask an admin to
    # invite you, so let's limit the access a bit
    if (not team.is_by_invitation() and not
        team.user_is_member(request.user)):
        return HttpResponseForbidden()
    return render(request, 'new-teams/admin-list.html', {
        'team': team,
        'admins': (team.members
                   .filter(Q(role=TeamMember.ROLE_ADMIN)|
                           Q(role=TeamMember.ROLE_OWNER))
                   .select_related('user'))
    })

@team_view
def activity(request, team, tab):
    try:
        page = int(request.GET['page'])
    except (ValueError, KeyError):
        page = 1
    activity_list = fetch_actions_for_activity_page(team, tab, page,
                                                    request.GET)
    language_choices = None
    if tab == 'videos':
        readable_langs = TeamLanguagePreference.objects.get_readable(team)
        language_choices = [(code, name) for code, name in get_language_choices()
                            if code in readable_langs]
    action_types = Action.TYPES_CATEGORIES[tab]

    has_more = len(activity_list) >= ACTIONS_PER_PAGE

    filtered = bool(set(request.GET.keys()).intersection([
        'action_type', 'language', 'sort']))

    next_page_query = request.GET.copy()
    next_page_query['page'] = page + 1

    context = {
        'activity_list': activity_list,
        'filtered': filtered,
        'action_types': action_types,
        'language_choices': language_choices,
        'team': team,
        'user': request.user,
        'next_page': page + 1,
        'next_page_query': next_page_query.urlencode(),
        'tab': tab,
        'has_more': has_more,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Activity')),
        ],
    }
    if team.is_old_style():
        template_dir = 'teams/'
    else:
        template_dir = 'new-teams/'

    if not request.is_ajax():
        return render(request, template_dir + 'activity.html', context)
    else:
        # for ajax requests we only want to return the activity list, since
        # that's all that the JS code needs.
        return render(request, template_dir + '_activity-list.html', context)

@team_view
def statistics(request, team, tab):
    """For the team activity, statistics tabs
    """
    if (tab == 'teamstats' and
        not permissions.can_view_stats_tab(team, request.user)):
        return HttpResponseForbidden("Not allowed")
    cache_key = 'stats-' + team.slug + '-' + tab
    cached_context = cache.get(cache_key)
    if cached_context:
        context = pickle.loads(cached_context)
    else:
        context = compute_statistics(team, stats_type=tab)
        cache.set(cache_key, pickle.dumps(context), 60*60*24)
    context['tab'] = tab
    context['team'] = team
    context['breadcrumbs'] = [
        BreadCrumb(team, 'teams:dashboard', team.slug),
        BreadCrumb(_('Activity')),
    ]
    if team.is_old_style():
        return render(request, 'teams/statistics.html', context)
    else:
        return render(request, 'new-teams/statistics.html', context)


def dashboard(request, slug):
    team = get_object_or_404(
        Team.objects.for_user(request.user, exclude_private=False),
        slug=slug)
    if not team.is_old_style() and not team.user_is_member(request.user):
        return welcome(request, team)
    else:
        return team.new_workflow.dashboard_view(request, team)

def welcome(request, team):
    if team.is_visible:
        videos = team.videos.order_by('-id')[:2]
    else:
        videos = None

    if Application.objects.open(team, request.user):
        messages.info(request,
                      _(u"Your application has been submitted. "
                        u"You will be notified of the team "
                        "administrator's response"))

    return render(request, 'new-teams/welcome.html', {
        'team': team,
        'join_mode': team.get_join_mode(request.user),
        'team_messages': team.get_messages([
            'pagetext_welcome_heading',
        ]),
        'videos': videos,
    })

@team_settings_view
def settings_basic(request, team):
    if team.is_old_style():
        return old_views.settings_basic(request, team)

    if permissions.can_rename_team(team, request.user):
        FormClass = forms.RenameableSettingsForm
    else:
        FormClass = forms.SettingsForm

    if request.POST:
        form = FormClass(request.POST, request.FILES, instance=team)

        is_visible = team.is_visible

        if form.is_valid():
            try:
                form.save()
            except:
                logger.exception("Error on changing team settings")
                raise

            if is_visible != form.instance.is_visible:
                tasks.update_video_public_field.delay(team.id)
                tasks.invalidate_video_visibility_caches.delay(team)

            messages.success(request, _(u'Settings saved.'))
            return HttpResponseRedirect(request.path)
    else:
        form = FormClass(instance=team)

    return render(request, "new-teams/settings.html", {
        'team': team,
        'form': form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Settings')),
        ],
    })

@team_settings_view
def settings_messages(request, team):
    if team.is_old_style():
        return old_views.settings_messages(request, team)

    initial = team.settings.all_messages()
    if request.POST:
        form = forms.GuidelinesMessagesForm(request.POST, initial=initial)

        if form.is_valid():
            for key, val in form.cleaned_data.items():
                setting, c = Setting.objects.get_or_create(team=team, key=Setting.KEY_IDS[key])
                setting.data = val
                setting.save()

            messages.success(request, _(u'Guidelines and messages updated.'))
            return HttpResponseRedirect(request.path)
    else:
        form = forms.GuidelinesMessagesForm(initial=initial)

    return render(request, "new-teams/settings-messages.html", {
        'team': team,
        'form': form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Settings'), 'teams:settings_basic', team.slug),
            BreadCrumb(_('Messages')),
        ],
    })

@team_settings_view
def settings_feeds(request, team):
    if team.is_old_style():
        return old_views.video_feeds(request, team)

    action = request.POST.get('action')
    if request.method == 'POST' and action == 'import':
        feed = get_object_or_404(team.videofeed_set, id=request.POST['feed'])
        feed.update()
        messages.success(request, _(u'Importing videos now'))
        return HttpResponseRedirect(request.build_absolute_uri())
    if request.method == 'POST' and action == 'delete':
        feed = get_object_or_404(team.videofeed_set, id=request.POST['feed'])
        feed.delete()
        messages.success(request, _(u'Feed deleted'))
        return HttpResponseRedirect(request.build_absolute_uri())

    if request.method == 'POST' and action == 'add':
        add_form = forms.AddTeamVideosFromFeedForm(team, request.user,
                                                   data=request.POST)
        if add_form.is_valid():
            add_form.save()
            messages.success(request, _(u'Video Feed Added'))
            return HttpResponseRedirect(request.build_absolute_uri())
    else:
        add_form = forms.AddTeamVideosFromFeedForm(team, request.user)

    return render(request, "new-teams/settings-feeds.html", {
        'team': team,
        'add_form': add_form,
        'feeds': team.videofeed_set.all(),
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Settings'), 'teams:settings_basic', team.slug),
            BreadCrumb(_('Video Feeds')),
        ],
    })

@team_settings_view
def settings_projects(request, team):
    if team.is_old_style():
        return old_views.settings_projects(request, team)

    projects = Project.objects.for_team(team)

    form = request.POST.get('form')

    if request.method == 'POST' and form == 'add':
        add_form = forms.ProjectForm(team, data=request.POST)

        if add_form.is_valid():
            add_form.save()
            messages.success(request, _('Project added.'))
            return HttpResponseRedirect(
                reverse('teams:settings_projects', args=(team.slug,))
            )
    else:
        add_form = forms.ProjectForm(team)

    if request.method == 'POST' and form == 'edit':
        edit_form = forms.EditProjectForm(team, data=request.POST)

        if edit_form.is_valid():
            edit_form.save()
            messages.success(request, _('Project updated.'))
            return HttpResponseRedirect(
                reverse('teams:settings_projects', args=(team.slug,))
            )
    else:
        edit_form = forms.EditProjectForm(team)

    if request.method == 'POST' and form == 'delete':
        try:
            project = projects.get(id=request.POST['project'])
        except Project.DoesNotExist:
            pass
        else:
            project.delete()
            messages.success(request, _('Project deleted.'))
            return HttpResponseRedirect(
                reverse('teams:settings_projects', args=(team.slug,))
            )

    return render(request, "new-teams/settings-projects.html", {
        'team': team,
        'projects': projects,
        'add_form': add_form,
        'edit_form': edit_form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Settings'), 'teams:settings_basic', team.slug),
            BreadCrumb(_('Projects')),
        ],
    })

@team_settings_view
def edit_project(request, team, project_slug):
    if team.is_old_style():
        return old_views.edit_project(request, team, project_slug)

    project = get_object_or_404(Project, slug=project_slug)
    if 'delete' in request.POST:
        project.delete()
        return HttpResponseRedirect(
            reverse('teams:settings_projects', args=(team.slug,))
        )
    elif request.POST:
        form = forms.ProjectForm(team, instance=project, data=request.POST)

        if form.is_valid():
            form.save()
            return HttpResponseRedirect(
                reverse('teams:settings_projects', args=(team.slug,))
            )
    else:
        form = forms.ProjectForm(team, instance=project)

    return render(request, "new-teams/settings-projects-edit.html", {
        'team': team,
        'form': form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Settings'), 'teams:settings_basic', team.slug),
            BreadCrumb(_('Projects'), 'teams:settings_projects', team.slug),
            BreadCrumb(project.name),
        ],
    })

@team_settings_view
def settings_workflows(request, team):
    return team.new_workflow.workflow_settings_view(request, team)
