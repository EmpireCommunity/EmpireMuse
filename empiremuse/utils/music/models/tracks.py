# -*- coding: utf-8 -*-
from __future__ import annotations

import uuid
from typing import Optional, List
from urllib import parse
from urllib.parse import quote, quote_plus


import empiremuse.wavelink as wavelink
from empiremuse.utils.music.converters import fix_characters, md_link, md_code
from empiremuse.utils.music.live_mark import is_live


class PartialPlaylist:
    __slots__ = ("data", "url", "tracks")

    def __init__(self, data: dict, url: str):
        self.data = data
        self.url = url
        self.tracks = []

    @property
    def name(self):
        try:
            return self.data["playlistInfo"]["name"]
        except KeyError:
            return

    @property
    def uri(self) -> str:
        return self.url

    @property
    def thumb(self):
        try:
            return self.data["playlistInfo"]["thumb"]
        except KeyError:
            pass
        try:
            return self.tracks[0].thumb
        except Exception:
            return ""


class PartialTrack:
    __slots__ = ("id", "info", "playlist", "unique_id", "ytid")

    def __init__(
        self,
        *,
        uri: str = "",
        title: str = "",
        author="",
        thumb: str = "",
        duration: int = 0,
        requester: int = 0,
        track_loops: int = 0,
        source_name: str = "",
        autoplay: bool = False,
        identifier: str = "",
        info: dict = None,
        playlist: PartialPlaylist = None,
        ytid="",
    ):
        self.info = info or {
            "author": fix_characters(author)[:97],
            "identifier": identifier,
            "title": title[:97],
            "uri": uri,
            "length": duration,
            "isStream": False,
            "isSeekable": True,
            "sourceName": source_name,
            "is_partial": True,
            "extra": {"requester": requester, "track_loops": track_loops, "thumb": thumb, "autoplay": autoplay},
        }

        self.id = None
        self.ytid = ytid
        self.unique_id = str(uuid.uuid4().hex)[:10]
        self.playlist: Optional[PartialPlaylist] = playlist

    def __repr__(self):
        return f"{self.info['sourceName']} - {self.duration} - {self.authors_string} - {self.title}"

    def __str__(self):
        return f"{self.info['sourceName']} - {self.duration} - {self.authors_string} - {self.title}"

    @property
    def thumb(self) -> str:
        try:
            return self.info["extra"]["thumb"] or self.info["artworkUrl"]
        except KeyError:
            try:
                return self.info["artworkUrl"]
            except KeyError:
                return ""

    @property
    def uri(self) -> str:
        return self.info["uri"] or self.search_uri

    @property
    def url(self) -> str:
        return self.uri

    @property
    def search_uri(self):
        return f"https://www.youtube.com/results?search_query={quote_plus((self.author + '-' + self.single_title) if self.info['sourceName'] not in ('youtube', 'soundcloud') else self.title)}"

    @property
    def title(self) -> str:
        return f"{self.author} - {self.single_title}"

    @property
    def name(self) -> str:
        return self.title

    @property
    def identifier(self) -> str:
        try:
            return self.info["identifier"]
        except KeyError:
            try:
                return self.info["extra"]["original_id"]
            except KeyError:
                return ""

    @property
    def source_name(self):
        return self.info["sourceName"] or "unkown"

    @property
    def single_title(self) -> str:
        return self.info["title"]

    @property
    def author(self) -> str:
        return self.info["author"] or "Artista Desconhecido"

    @property
    def authors_string(self) -> str:
        try:
            return ", ".join(self.info["extra"]["authors"])
        except KeyError:
            return self.author

    @property
    def authors_md(self) -> str:
        try:
            return self.info["extra"]["authors_md"]
        except KeyError:
            return md_code(self.author)

    @property
    def authors(self) -> List[str]:
        try:
            return self.info["extra"]["authors"]
        except KeyError:
            return [self.author]

    @property
    def lyrics(self) -> str:
        try:
            return self.info["extra"]["lyrics"]
        except KeyError:
            return ""

    @property
    def requester(self) -> int:
        return self.info["extra"]["requester"]

    @property
    def autoplay(self) -> bool:
        try:
            return self.info["extra"]["autoplay"]
        except KeyError:
            return False

    @property
    def track_loops(self) -> int:
        return self.info["extra"]["track_loops"]

    @property
    def is_stream(self) -> bool:
        return self.info["isStream"]

    @property
    def duration(self) -> int:
        return self.info["length"]

    @property
    def album_name(self) -> str:
        try:
            return self.info["extra"]["album"]["name"]
        except KeyError:
            try:
                self.info["pluginInfo"]["albumName"]
            except KeyError:
                return ""

    @property
    def album_url(self) -> str:
        try:
            return self.info["extra"]["album"]["url"]
        except KeyError:
            try:
                self.info["pluginInfo"]["albumUrl"]
            except KeyError:
                return ""

    @property
    def playlist_name(self) -> str:
        try:
            return self.playlist.name[:97]
        except AttributeError:
            return ""

    @property
    def playlist_url(self) -> str:
        try:
            return self.playlist.url
        except AttributeError:
            return ""


class LavalinkPlaylist:
    __slots__ = ("data", "url", "tracks")

    def __init__(self, data: dict, **kwargs):
        self.data = data
        self.url = kwargs.pop("url")

        try:
            self.data["playlistInfo"]["thumb"] = kwargs["pluginInfo"]["artworkUrl"]
        except KeyError:
            pass

        try:
            self.data["playlistInfo"]["type"] = kwargs["pluginInfo"]["type"]
        except KeyError:
            pass

        encoded_name = kwargs.pop("encoded_name", "track")

        try:
            if self.data["tracks"][0]["info"].get("sourceName") == "youtube":
                try:
                    self.url = f"https://www.youtube.com/playlist?list={parse.parse_qs(parse.urlparse(self.url).query)['list'][0]}"
                except KeyError:
                    pass
        except IndexError, KeyError:
            pass

        pluginInfo = kwargs.pop("pluginInfo", {})
        thumb = None
        try:
            playlist = self if pluginInfo["type"] == "playlist" else None
            if pluginInfo["type"] == "album":
                thumb = self.thumb
        except KeyError:
            playlist = self
        self.tracks = [
            LavalinkTrack(
                id_=track[encoded_name],
                info=track["info"],
                pluginInfo=track.get("pluginInfo") or pluginInfo,
                thumb=thumb,
                playlist=playlist,
                **kwargs,
            )
            for track in data.get("tracks", [])
        ]

    @property
    def uri(self):
        return self.url

    @property
    def name(self):
        return self.data["playlistInfo"]["name"]

    @property
    def thumb(self):
        try:
            return self.data["playlistInfo"]["thumb"]
        except KeyError:
            pass
        try:
            return self.tracks[0].thumb
        except Exception:
            return ""


class LavalinkTrack(wavelink.Track):
    __slots__ = ("extra", "playlist", "unique_id")

    def __init__(self, *args, **kwargs):
        try:
            args[1]["title"] = fix_characters(args[1]["title"])[:97]
        except IndexError:
            pass
        super().__init__(*args, **kwargs)
        self.title = fix_characters(self.title)
        self.info["title"] = self.title
        self.unique_id = str(uuid.uuid4().hex)[:10]

        try:
            self.info["sourceName"]
        except Exception:
            self.info["sourceName"] = "LavalinkTrack"

        try:
            if (albumname := fix_characters(self.info["pluginInfo"]["albumName"])) == self.title:
                del self.info["pluginInfo"]["albumName"]
                del self.info["pluginInfo"]["albumUrl"]
            else:
                self.info["pluginInfo"]["albumName"] = albumname
        except AttributeError, KeyError:
            pass

        try:
            self.info["extra"]
        except KeyError:
            self.info["extra"] = {}

        self.info["extra"].update(
            {
                "track_loops": kwargs.pop("track_loops", 0),
                "requester": kwargs.pop("requester", ""),
                "autoplay": kwargs.pop("autoplay", ""),
            }
        )

        self.playlist: Optional[LavalinkPlaylist] = kwargs.pop("playlist", None)

        if self.info["sourceName"] == "youtube":
            self.info["artworkUrl"] = f"https://img.youtube.com/vi/{self.ytid}/mqdefault.jpg"
            if "list=" not in self.uri:
                try:
                    self.info["uri"] = (
                        f"{self.uri}&list={parse.parse_qs(parse.urlparse(self.playlist_url or self.album_url).query)['list'][0]}"
                    )
                except KeyError:
                    pass

        elif self.info["sourceName"] == "soundcloud":
            self.info["artworkUrl"] = (
                self.info.get("artworkUrl", "").replace("-large.", "-t500x500.").replace("-original.", "-t500x500.")
            )

            if "?in=" not in self.uri:
                try:
                    self.info["uri"] = f"{self.uri}?in=" + self.playlist_url.split("soundcloud.com/")[1]
                except Exception:
                    pass

        elif thumb := kwargs.get("thumb"):
            self.info["artworkUrl"] = thumb

    def __repr__(self):
        return f"{self.info['sourceName']} - {self.duration if not self.is_stream else 'stream'} - {self.authors_string} - {self.title}"

    @property
    def thumb(self) -> str:
        return self.info["artworkUrl"] or ""

    @property
    def source_name(self):
        return self.info["sourceName"] or "unkown"

    @property
    def name(self) -> str:
        return self.title

    @property
    def single_title(self) -> str:
        return self.title

    @property
    def url(self) -> str:
        return self.info["uri"]

    @property
    def search_uri(self):
        if is_live(self):
            return ""  # a estação ao vivo da Jukebox não tem link nenhum nas telas, nem a busca do YouTube (A6, Alicerce m4)
        return f"https://www.youtube.com/results?search_query={quote(self.title)}"

    @property
    def authors_md(self) -> str:
        try:
            if self.info["pluginInfo"]["artistUrl"]:
                return md_link(self.author, self.info["pluginInfo"]["artistUrl"])
        except KeyError:
            pass
        return md_code(self.author)

    @property
    def authors(self) -> List[str]:
        return [f"{self.author}"]

    @property
    def authors_string(self) -> str:
        return f"{self.author}"

    @property
    def album_name(self) -> str:
        try:
            return self.info["extra"]["album"]["name"]
        except KeyError:
            try:
                return self.info["pluginInfo"]["albumName"]
            except KeyError:
                return ""

    @property
    def album_url(self) -> str:
        try:
            return self.info["extra"]["album"]["url"]
        except KeyError:
            try:
                return self.info["pluginInfo"]["albumUrl"]
            except KeyError:
                return ""

    @property
    def lyrics(self) -> str:
        try:
            return self.info["extra"]["lyrics"]
        except KeyError:
            return ""

    @property
    def requester(self) -> int:
        return self.info["extra"]["requester"]

    @property
    def autoplay(self) -> bool:
        try:
            return self.info["extra"]["autoplay"]
        except KeyError:
            return False

    @property
    def track_loops(self) -> int:
        return self.info["extra"]["track_loops"]

    @property
    def playlist_name(self) -> str:
        try:
            return self.playlist.name[:97]
        except AttributeError:
            return ""

    @property
    def playlist_url(self) -> str:
        try:
            return self.playlist.url
        except AttributeError:
            return ""
