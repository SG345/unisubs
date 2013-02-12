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

import logging
from time import sleep

from django.conf import settings
import gdata

from apps.videos.models import VideoUrl, Video, VIDEO_TYPE_YOUTUBE
from apps.videos.types import video_type_registrar, UPDATE_VERSION_ACTION
from apps.videos.types.youtube import YouTubeApiBridge
from apps.accountlinker.models import ThirdPartyAccount
from apps.accountlinker.models import (
    translate_string, AMARA_DESCRIPTION_CREDIT
)


UPLOAD_URI_BASE = 'http://gdata.youtube.com/feeds/api/users/default/uploads/%s'
YOUTUBE_API_SECRET  = getattr(settings, "YOUTUBE_API_SECRET", None)


logger = logging.getLogger(__name__)


class Remover(object):
    """
    Responsible for removing Amara credit from Youtube video descriptions

    tpa = ThirdPartyAccount.objects.get(pk=tpa_pk)
    remover = Remover(tpa)
    remover.remove_all()
    """

    def __init__(self, third_party_account):
        self.tpa = third_party_account

    def _resync_subs_for_video(self, video):
        languages = video.subtitlelanguage_set.all()
        logger.info(video)

        for language in languages:
            latest_version = language.latest_version(public_only=True)

            if latest_version:
                logger.info(' ' + str(language))
            else:
                logger.info('  no version for:' + str(language))
                continue

            ThirdPartyAccount.objects.mirror_on_third_party(video,
                    language, UPDATE_VERSION_ACTION,
                    version=latest_version)
        return video.video_id

    def _fix_video(self, vurl):
        video = vurl.video
        language_code = video.language

        if not language_code:
            language_code = 'en'

        vt = video_type_registrar.video_type_for_url(vurl.url)

        username = vurl.owner_username
        account = ThirdPartyAccount.objects.get(type=vurl.type,
                username=username)

        bridge = YouTubeApiBridge(account.oauth_access_token,
                                account.oauth_refresh_token,
                                vt.videoid)

        video_url = video.get_absolute_url()

        uri = UPLOAD_URI_BASE % bridge.youtube_video_id
        entry = bridge.GetVideoEntry(uri=uri)
        entry = entry.to_string()
        entry = gdata.youtube.YouTubeVideoEntryFromString(entry)

        current_description = entry.media.description.text

        # For some reason the above video.get_absolute_url() call didn't
        # include the /en/ prefix.
        unisubs_video_url = "http://www.universalsubtitles.org/en%s" % video_url
        amara_video_url = "http://www.amara.org/en%s" % video_url

        unisubs_supposed_credit = self._get_supposed_credit(unisubs_video_url,
                language_code)
        amara_supposed_credit = self._get_supposed_credit(amara_video_url,
                language_code)

        credits = (amara_supposed_credit, unisubs_supposed_credit,)

        if not current_description.startswith(credits):
            logger.info("%s doesn't have desc credit" % vurl.url)
            return video.video_id

        if current_description.startswith(amara_supposed_credit):
            new_description = current_description.replace(
                    amara_supposed_credit, '')

        if current_description.startswith(unisubs_supposed_credit):
            new_description = current_description.replace(
                    unisubs_supposed_credit, '')

        entry.media.description.text = new_description
        entry = entry.ToString()

        status_code = bridge._make_update_request(uri, entry)

        if status_code == 401:
            bridge.refresh()
            status_code = bridge._make_update_request(uri, entry)

        if status_code == 200:
            logger.info('%s success' % vurl.url)
            return video.video_id

        logger.info('FAIL %s' % vurl.url)

    def _get_supposed_credit(self, vurl, language='en'):
        credit = translate_string(AMARA_DESCRIPTION_CREDIT, language)
        return "%s: %s\n\n" % (credit, vurl)

    def _sleep(self):
        logger.info('Pausing')
        sleep(3)

    def _remove_descs(self, vurls):
        logger.info('%s video descriptions to process' % len(vurls))

        for vurl in vurls:
            logger.info('Starting to process video %s' % vurl.video.video_id)
            try:
                # video_id = self._fix_video(vurl)
                self._fix_video(vurl)
                logger.info('Done processing video')
            except Exception, e:
                logger.info(e)
            logger.info('Done processing video')
            self._sleep()

    def _resync_videos(self, videos):
        for video in videos:
            logger.info('Start resync for %s' % video.video_id)
            try:
                # video_id = self._resync_subs_for_video(video)
                self._resync_subs_for_video(video)
            except Exception, e:
                logger.info(e)
            logger.info('End resync')
            self._sleep()

    def remove_for_video(self, video_id):
        try:
            video = Video.objects.get(video_id=video_id)
        except Video.DoesNotExist:
            raise Exception("Video doesn't exist")

        yt_urls = video.videourl_set.filter(type=VIDEO_TYPE_YOUTUBE)

        if not yt_urls.exists():
            raise Exception("Not a Youtube video.")

        for vurl in yt_urls:
            try:
                self._fix_video(vurl)
            except Exception, e:
                logger.info(e)

        self._resync_subs_for_video(video)

    def remove_all(self):
        vurls = VideoUrl.objects.filter(
                type=VIDEO_TYPE_YOUTUBE,
                video__owner_username=self.tpa.username)
        self._remove_descs(vurls)

    def remove_for_team_videos(self, team_videos):
        vurls = VideoUrl.objects.filter(
                type=VIDEO_TYPE_YOUTUBE,
                video__teamvideo__in=team_videos,
                video__owner_username=self.tpa.username)
        self._remove_descs(vurls)

    def remove_for_team(self, team):
        logger.info('Only processing videos for %s' % team.slug)
        vurls = VideoUrl.objects.filter(
                type=VIDEO_TYPE_YOUTUBE,
                video__teamvideo__team=team,
                video__owner_username=self.tpa.username)
        self._remove_descs(vurls)
