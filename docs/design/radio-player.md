# Painel do player de rádio: desenho reutilizável

Especificação do painel que o bot posta no chat da call de rádio: a **mensagem no Discord** (embed com a imagem do visor, botões só
com emoji e dois selects) e o **visor** (PNG gerado pelo bot a cada atualização), em duas skins idênticas às referências do dono
(rádio de carro: cartão branco e cartão preto). Serve para reproduzir o mesmo desenho em outro lugar (site, prévias de recursos,
outros painéis). O código que implementa é `empiremuse/utils/radio/visor.py` (renderizador) e
`empiremuse/utils/music/skins/radio_player/` (skins); os textos vêm do catálogo (`visor.*` em `empiremuse/locales/*/bot.json`).

Decisões do dono que moldam tudo (brief do rework da rádio, P1-P8): nada repetido em texto no embed (tudo vive no visor); logo da
estação dentro do visor, à direita do nome; ícones do kit mnauliady (identidade do bot) e tipografia Inter; botões 1×5 ou 2×5
simétricos; o select de filtro tem a entrada **Favoritas**; a escala de sintonia é a posição na lista; duas skins (branca e preta)
no sistema de skins.

## 1. Tokens

### 1.1 Tipografia

| Papel | Fonte | Tamanho (px lógicos) | Observação |
|---|---|---|---|
| Nome da estação | Inter Bold | 20 | cortado com reticências para caber ao lado do logo |
| "FM" | Inter Bold | 12 | marca fixa do visor, como a banda num rádio |
| Taxa e codec | Inter Medium | 9.5 | `128 kbps · MP3` |
| Chips (AO VIVO, posição) | Inter SemiBold | 9 | |
| Rótulo pequeno (TOCANDO AGORA, FILTRO) | Inter SemiBold 8 / Medium 9.5 | | tracking 1.2 / 0.8 px |
| Título da música | Inter SemiBold | 14 | |
| Artista e avisos | Inter Regular | 10.5 | |
| Pílula da lista (ESTAÇÕES · 12) | Inter SemiBold | 11 | tracking 0.6 |
| Cabeçalho da direita (A seguir, Volume) | Inter SemiBold | 12.5 / 11.5 | |
| Pílulas das próximas estações | Inter Medium 11.5 / SemiBold 11.5 | | nome / taxa |
| Números da escala | Inter Medium | 8.5 | |

A imagem é desenhada em **2×** (1320×432) e o Discord reduz; os tamanhos acima são em 1× (660×216). Inter é SIL OFL
(`assets/fonts/LICENSE-Inter.txt`); substitui a Helvetica Now das referências, que é licenciada. SF Symbols (só Apple) não entra:
os ícones dos botões são os app emojis do kit mnauliady e os do visor são traços monocromáticos desenhados pelo renderizador.

### 1.2 Cores das skins

Medidas por amostragem de pixels nas referências. `black` é a padrão.

| Token | black | white | Onde |
|---|---|---|---|
| `card` | `#1E1E1E` | `#E7E7E7` | fundo do cartão (raio 18) |
| `card_border` | `#151515` | `#D2D2D2` | contorno 1.5 px |
| `lcd` | `#67B095` | `#A5C4DE` | visor da estação (raio 8); fora do ar: 75% |
| `lcd_text` / `lcd_dim` | `#0E1814` / `#1E4637` | `#141416` / `#465666` | nome / detalhes |
| `chip_dark` / `chip_dark_text` | `#111111` / `#F0F0F0` | `#191919` / `#FFFFFF` | chip de estado |
| `chip_light` / `chip_light_text` | `#F0F0F0` / `#111111` | `#FFFFFF` / `#191919` | chip da posição |
| `np` / `np_text` / `np_dim` / `np_chip` | `#111111` / `#FAFAFA` / `#969696` / `#282828` | `#191919` / `#FFFFFF` / `#AAAAAA` / `#323232` | painel "tocando agora" |
| `scale_line` / `scale_tick` / `scale_text` / `pointer` | `#5A5A5A` / `#6E6E6E` / `#BEBEBE` / `#FAFAFA` | `#787878` / `#8C8C8C` / `#3C3C3C` / `#191919` | escala de sintonia |
| `pill` / `pill_text` | `#111111` / `#F0F0F0` | `#D3D3D3` / `#191919` | pílula ESTAÇÕES · n |
| `round_btn` / `round_icon` | `#111111` / `#858585` | `#D3D3D3` / `#1F1F1F` | botões redondos (setas, estrela, sinal) |
| `divider` | `#343434` | `#FEFEFE` | divisórias |
| `box` / `box_border` / `box_label` / `box_text` | `#1E1E1E` / `#535353` / `#969696` / `#FAFAFA` | `#F2F2F2` / `#CDCDCD` / `#5A5A5A` / `#191919` | caixa FILTRO |
| `side_btn` / `side_btn_text` | `#111111` / `#C8C8C8` | `#D3D3D3` / `#191919` | botão RESERVA |
| `heading` / `heading_dim` | `#FAFAFA` / `#8C8C8C` | `#191919` / `#6E6E6E` | A seguir, Volume |
| `st_pill` / `st_pill_text` / `st_pill_dim` | `#333333` / `#FAFAFA` / `#AAAAAA` | `#FFFFFF` / `#191919` / `#787878` | próximas estações |
| `slider` / `slider_fill` | `#101010` / `#6AB096` | `#CFCFCF` / `#71AFE2` | volume |
| `slider_knob` / `slider_knob_border` | `#FAFAFA` / `#6AB096` | `#FFFFFF` / `#71AFE2` | cursor do volume |
| `transport` / `transport_dim` | `#FAFAFA` / `#6E6E6E` | `#191919` / `#969696` | legenda dos 5 botões |
| `knob` / `knob_ring` / `knob_icon` / `knob_pm` | `#FAFAFA` / `#3C3C3C` / `#000000` / `#C8C8C8` | `#1F1F1F` / `#5A5A5A` / `#FFFFFF` / `#3C3C3C` | botão giratório |
| `live` | `#E11D48` | `#E11D48` | ponto de AO VIVO (vermelho Nebulosa) |

Qualidade (R5), igual nas duas skins: boa `#22C55E`, ok `#EAB308`, fraca `#F97316`, falhando `#EF4444`. Os limiares são do
lado do bot (proposta do Turbine no PR): boa ≥ 128 kbps com checagem ok nas últimas 24 h; ok 64-127; fraca < 64 ou checagem
velha; falhando = falha recente no health check.

### 1.3 Espaçamento e formas

Cartão 660×216, raio 18, margem interna 14. Divisória vertical em x = 344. Raios: visor e painel 8, caixas 6, chips 3, pílulas 14,
botões redondos r = 14 (linha de baixo) e r = 8 (setas da escala). Botão giratório r = 26 com anel r = 30.

## 2. Layout

```
┌──────────────────────────────────────────┬─────────────────────────────────────┐
│ [AO VIVO][3]              │ TOCANDO AGORA│ FILTRO   Brasil › São Paulo ›│RESERVA│
│ Jovem Pan          (logo) │ Evidências   │ A seguir · 12            ● Boa      │
│ FM 128 kbps · MP3         │ Chitãozinho… │ [● 4. Kiss FM   192][● 5. Nova… 64] │
│                           │     [⏱ 45 min]│─────────────────────────────────────│
│ ‹ |..|..|..|..|..|..|..| › │              │ Volume ───────●──── 80        (+)   │
│   1  2  3  4  5  6  7  8  9 10 11 12     │ ⏮ 🔉 🔇 🔊 ⏭                ( 🔊 )   │
│ (★) [     ESTAÇÕES · 12     ] (📶)       │                            (−)      │
└──────────────────────────────────────────┴─────────────────────────────────────┘
```

Metade esquerda (o "rádio"):

1. **Visor LCD** (14,14)→(176,110): chip escuro do estado (`AO VIVO` com ponto vermelho, `MUDO`, `FORA DO AR`) e chip claro com
   a posição na lista; nome da estação em destaque com o **logo** (24 px, círculo) à direita; `FM` + taxa e codec.
2. **Painel "tocando agora"** (184,14)→(330,110): rótulo, título, artista e o chip do tempo no ar (`⏱ 45 min`). Nos estados
   silenciada e fora do ar o painel vira o aviso do estado (ver §3).
3. **Escala de sintonia** (y = 138): uma posição por estação da lista ativa (até 25; acima disso só a linha e o marcador),
   marcador na atual, setas redondas nas pontas (anterior/próxima).
4. **Linha de baixo** (y = 182): estrela (acesa quando a estação é favorita do servidor), pílula da lista ativa
   (`ESTAÇÕES · n` ou `FAVORITAS · n`) e o ícone de sinal na cor da qualidade.

Metade direita (os "ajustes"):

5. **Caixa FILTRO** (no lugar de "RADIO AREA" da referência): o caminho do filtro (`Brasil › São Paulo`) ou `Favoritas do servidor`.
6. **Botão RESERVA** (no lugar de "STEREO"): fone + `RESERVA n` aceso quando a reserva está no ar; `SEM RESERVA` apagado.
7. **A seguir · n** com a qualidade da estação atual à direita e as **duas próximas** estações em pílulas (ponto de qualidade,
   posição, nome, taxa).
8. **Volume**: deslizante com o cursor e o valor (`80` ou `mudo`). O valor é o volume do player (10 a 150, 100 = padrão); o
   preenchimento do deslizante é a fração de 150 (`volume_level`).
9. **Legenda de transporte**: os cinco ícones da primeira fileira de botões, apagados os que não valem no estado (mudo aceso só
   quando silenciada).
10. **Botão giratório**: alto-falante (ou mudo) com `+` e `−`, como na referência.

## 3. Estados

| Estado | Visor LCD | Painel da direita do visor | Legenda / giratório |
|---|---|---|---|
| `live` | `● AO VIVO` + posição; nome normal | TOCANDO AGORA, título, artista, tempo no ar | ícones normais; alto-falante |
| `muted` | `MUDO`; nome na cor `lcd_dim` | SILENCIADA / Em silêncio / Aperte mudo de novo | só o mudo aceso; giratório com mudo; volume `mudo` |
| `offline` | `FORA DO AR`; LCD a 75% | SEM SINAL / Reconectando… / Nova tentativa em n s | sinal vermelho (`failing`) |
| lista = favoritas | posição na lista de favoritas | pílula `FAVORITAS · n`, FILTRO `Favoritas do servidor`, estrela acesa | |
| sem reserva | | botão RESERVA apagado (`SEM RESERVA`) | |
| texto longo / hostil | nome, título, artista, filtro e próximas passam por `clean_text` (sem controle, formatação ou quebra) e são cortados com `…` | | |

Sem título do fluxo, o painel mostra o nome da estação no lugar do título. Logo inválido vira um disco na cor `lcd_dim`.

## 4. A mensagem no Discord

- **Embed**: cor violeta Nebulosa (`#8B5CF6`), só a imagem do visor (anexo `visor.png`) e o rodapé "Só quem está na call da
  rádio usa os controles". Nenhum texto repetido (P1).
- **Botões** (2×5, só emoji, app emojis do kit mnauliady; `custom_id` fixos de `rules.radio`):
  linha 1 `backward` estação anterior · `volume_down` volume − · `volume_mute` mudo (vermelho quando silenciada) · `volume` volume + ·
  `skip` próxima; linha 2 `star` favoritar · `notification` informações (efêmero) · `refresh` reconectar o fluxo · `shuffle` estação
  aleatória da lista · `stop` desligar (vermelho, com confirmação). Variante 1×5 = só a linha 1.
- **Select 1**: as estações da lista ativa (último filtro aplicado ou favoritas), já carregado, uma opção por estação com o ponto de
  qualidade 🟢🟡🟠🔴, posição, nome, taxa e cidade; a atual marcada como padrão.
- **Select 2**: `Favoritas do servidor · n` e os níveis do filtro (região › país › estado › cidade, a cidade pela busca por raio de
  100 km do R4). O último filtro fica lembrado por rádio.

## 5. Skins

Uma skin é um módulo em `empiremuse/utils/music/skins/radio_player/` com `load()` devolvendo `RadioSkin(name, preview, tokens)`;
`tokens` precisa ter todos os nomes de `TOKEN_NAMES` (teste em `tests/cogs/test_radio_visor.py`). `load_radio_skin(nome)` devolve
a padrão para um nome desconhecido (o valor vem do documento do servidor). Para criar uma skin nova: copie `black.py`, troque as
cores e a prévia, acrescente o nome em `SKIN_NAMES`. A escolha por servidor fica no documento da rádio (`skin`, padrão `black`).

## 6. Assets

| Asset | Caminho | Licença / origem |
|---|---|---|
| Inter Regular, Medium, SemiBold, Bold | `assets/fonts/Inter-*.ttf` | SIL OFL 1.1 (`assets/fonts/LICENSE-Inter.txt`) |
| Prévias das skins | `assets/skins/skin_radio_black_preview.png`, `skin_radio_white_preview.png` | geradas pelo renderizador |
| Ícone genérico da estação sem logo | `assets/radio/global.png` | do projeto |
| App emojis dos botões | `assets/emojis/{backward,volume,skip,star,notification,refresh,shuffle,stop}.png` | kit mnauliady (Flaticon), atribuição no `CREDITS.md` |
| App emojis a baixar | `volume_down.png`, `volume_mute.png` | mesmo kit `media-player-icons` da mnauliady |
| Referências do dono | `/var/tmp/maestro/refs-painel/ref1.png` (branca), `ref2.png` (preta) | fora do repositório |

## 7. Reaproveitar em outro lugar

O renderizador só depende de Pillow e dos tokens: `render_visor(VisorData(...), tr, skin)` devolve PNG; no bot,
`render_snapshot(snapshot, tr)` monta o `VisorData` a partir do `RadioPanelSnapshot` (`from_snapshot`) e usa a skin do documento. Para uma imagem de
divulgação (ideia do dono para o site), monte um `VisorData` com dados de exemplo e grave o resultado; o tradutor pode ser o de
qualquer idioma do catálogo. Para outro painel (por exemplo, um player de música em imagem), copie a estrutura: tokens por skin,
coordenadas em 1× desenhadas em 2×, texto de fora sempre por `clean_text` + corte com reticências, e um `NamedTuple` imutável como
entrada.

## Rádio personalizada no mesmo visor

O retrato (`custom_panel.custom_snapshot`) é o mesmo `RadioPanelSnapshot`; o renderizador recebe `custom=True`
(`render_snapshot(..., custom=True)`, `visor_payload(..., custom=True)`) e adapta só o que não existe na personalizada:

- Badge `WEB` no lugar de `FM`; sem codec nem taxa, a linha de detalhe diz `visor.custom.detail` ("Estação do servidor").
- O botão lateral da reserva vira o **tipo** filtrado (ícone de lista + último rótulo de `filter_path`) ou `visor.all_types`.
- Select 2 = `panel.type_select_options`: `Todas as estações` (`f:all`), `Gênero`, `Artistas`, `Playlists do servidor`
  (values `f:genre|f:artists|f:playlists`, rótulos `radio.station.kind.*`). Favoritas da personalizada: pendente do dono.
- O ícone da estação (disco de 24 px) fica no disco de reserva até a escolha do dono (comparação em
  `/var/tmp/maestro/custom-icons-comparison.md`).
- Nomes compridos de estação encolhem (Bold 20 → 17 → 15) antes de receber reticências; vale também para a ao vivo.
