from __future__ import annotations

import re

from streamlink.plugin import Plugin, pluginmatcher
from streamlink.stream.hls import HLSStream
from streamlink.exceptions import NoStreamsError
from streamlink.logger import getLogger

log = getLogger(__name__)


@pluginmatcher(
    name="live",
    pattern=re.compile(
        r"https?://vod\.tvp\.pl/live,\d+/[^,]+,(?P<channel_id>\d+)"
    ),
)
@pluginmatcher(
    name="vod",
    pattern=re.compile(
        r"https?://vod\.tvp\.pl/(?!live,)[^/]+/.+,(?P<vod_id>\d+)"
    ),
)
class TVP(Plugin):

    API = "https://vod.tvp.pl/api/products/{id}/videos/playlist"

    def _get_hls(self, url):
        try:
            streams = HLSStream.parse_variant_playlist(self.session, url)
        except Exception:
            return

        # remove alternate audio streams to prevent hls-multi
        for name, stream in streams.items():
            if "_alt" in name:
                continue
            yield name, stream

    def _get_streams(self):

        if self.matches["live"]:
            vid = self.match["channel_id"]
        elif self.matches["vod"]:
            vid = self.match["vod_id"]
        else:
            return

        url = self.API.format(id=vid)

        try:
            data = self.session.http.get(
                url,
                params={
                    "platform": "BROWSER",
                    "videoType": "LIVE",
                },
            ).json()
        except Exception as err:
            log.error(f"TVP API error: {err}")
            return

        if "code" in data:
            raise NoStreamsError(data["code"])

        sources = data.get("sources", {})
        hls = sources.get("HLS")

        if not hls:
            return

        hls_url = hls[0]["src"]

        yield from self._get_hls(hls_url)


__plugin__ = TVP
