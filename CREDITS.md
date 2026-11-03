# Créditos

## Origem do projeto

**EmpireMuse** é uma obra derivada do
[MuseHeart-MusicBot](https://github.com/zRitsu/MuseHeart-MusicBot), criado por **zRitsu** e
distribuído sob a licença **GPL-2.0** (consulte o arquivo [`LICENSE`](LICENSE), que permanece
intacto). Como obra derivada, o EmpireMuse também é distribuído sob a GPL-2.0.

- Repositório original: <https://github.com/zRitsu/MuseHeart-MusicBot>
- Ponto de partida do fork: commit upstream `32f227e9`
- Repositório do EmpireMuse: <https://github.com/EmpireCommunity/EmpireMuse>

## Agradecimentos

Nosso agradecimento a **zRitsu** e a todas as pessoas que contribuíram com o
MuseHeart-MusicBot upstream. Sem esse trabalho, o EmpireMuse não existiria.

## Ícones

Só 48 dos emojis de aplicação em `assets/emojis/` usam ícones da [Flaticon](https://www.flaticon.com/), licença gratuita com
atribuição obrigatória (kits `media-player-icons` e `web-ui-icons`, de mnauliady): `add_music`, `arrow_left`, `arrow_right`, `backward`, `cancel`, `comment`, `config`, `disc`, `drag`, `fast_backward`, `gear`,
`headset`, `history`, `like`, `lock`, `loved`, `lyrics`, `microphone`, `music_note`, `music_notes`, `music_queue`, `notification`,
`pause`, `pinned`, `play`, `play_source`, `playlist_add`, `playlist_play`, `playlists`, `playpause`, `plus`, `record`, `refresh`,
`repeat`, `repeat_all`, `replay`, `search`, `shuffle`, `skip`, `sound_spectrum`, `speaker`, `stagechannel`, `star`, `stop`, `tools`,
`user`, `voice_event` e `volume`.

<a href="https://www.flaticon.com/free-icons/user-interface" title="user interface icons">User interface icons created by mnauliady - Flaticon</a>

Os demais emojis (logotipos de plataformas como YouTube, Spotify, Deezer, SoundCloud, Twitch e Tidal, e os outros que não foram
trocados) não vêm desse kit e não fazem parte dessa licença.

## Dados geográficos

O filtro de local das rádios (região, país, estado e cidade) usa uma lista FIXA, gerada uma vez por `tools/radio_geo/build.py` e distribuída em
`empiremuse/utils/radio/data/locations.json`; nada é buscado em execução. A procedência (URL, data e sha256 de cada entrada) está em
`empiremuse/utils/radio/data/locations.meta.json`.

- **GeoNames** (`cities5000` e `admin1CodesASCII`): dados de localização de [GeoNames](https://www.geonames.org/), licença
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Os dados foram reduzidos (24 cidades por estado) e agrupados.
- **CLDR 46** (`territoryContainment`): [Unicode CLDR](https://cldr.unicode.org/), Unicode License v3 (regiões por sub-região M49).
- **iso-codes** (`iso_3166-2`): nomes e siglas das subdivisões, LGPL-2.1+, usados só na geração.
- **Radio Browser**: contagens de estações e geografia, domínio público.

## Recursos espelhados

Alguns recursos externos dos quais o projeto depende foram espelhados em repositórios da
organização EmpireCommunity, para não depender de contas de terceiros. A origem de cada um
é preservada abaixo.

| Recurso | Origem | Espelho / local no EmpireMuse | Licença / observações |
| :--- | :--- | :--- | :--- |
| Binários do Lavalink (`EmpireMuse-LL-binaries`, release `0.0.5`) | zRitsu (repositório upstream de binários `LL-binaries` na release `0.0.1`); Lavalink oficial v4.2.2 (equipe lavalink-devs) e plugins oficiais | [EmpireCommunity/EmpireMuse-LL-binaries](https://github.com/EmpireCommunity/EmpireMuse-LL-binaries) `0.0.5` | Lavalink 4.2.2 oficial (licença MIT) com plugins oficiais espelhados (LavaSrc 4.8.3, youtube-plugin snapshot, lyrics 2.7.1 (compilado do commit `d0dfbb13` do mantenedor, porque a tag 2.7.1 não compilava) e o plugin Soundy 1.1.0 de [lowkwrs/soundy](https://github.com/lowkwrs/soundy), licença MIT, compilado do commit `e2c5da8`); `application.yml` com cliente TV, OAuth desligado por padrão e log do `YoutubeOauth2Handler` em WARN; hashes verificados via SHA-256 no download; preserva o histórico das releases `0.0.1` a `0.0.3`. |
| Aplicativo de RPC (Rich Presence) | [zRitsu/MuseHeart-MusicBot-RPC-app](https://github.com/zRitsu/MuseHeart-MusicBot-RPC-app) | [EmpireCommunity/EmpireMuse-RPC-app](https://github.com/EmpireCommunity/EmpireMuse-RPC-app) | GPL-3.0. |
| Imagens em `assets/` | Upstream e serviços de terceiros | `assets/` | Substituição por arte própria planejada. |
| Lista de servidores Lavalink | Gist do upstream; servidores públicos via `lavalink.darrennathanael.com` | `data/lavalink_servers.ini` | O crédito consta no cabeçalho do arquivo. |
| Guias de uso | Wiki e gist do upstream | `docs/guides/` | Cada página traz a nota de adaptação com o link do upstream. |
