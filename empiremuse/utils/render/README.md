# `utils/render/`: desenho das imagens do bot

Base do desenho de imagem do 2.3: o disco da Jukebox C (`muse_jukebox`) e os painéis da rádio 24 h (visor e Retrato). O
contrato de implementação é privado; este README resume o que o código garante e o orçamento de memória.

## Módulos

| Módulo | Faz | Estado |
|---|---|---|
| `slot.py` | `RenderSlot`: uma vaga de desenho (`asyncio.Semaphore(1)`) com contador de espera; `run(fn, *args)` numa thread; `pressure()`; `RenderBusy` quando a espera passa de `SLOT_WAIT_S` | uma por processo (`shared_slot()`) |
| `budget.py` | os tetos nomeados (os de regra pura vêm de `rules/card.py` e são reexportados) | constantes |
| `gif.py` | `encode_png` (`compress_level=6`) | puro |
| `primitives.py` | ícones, logo em círculo (só PNG), `fit` por busca binária, texto recortado na caixa | puro |
| `fonts.py` | `cmap_points` (os caracteres que a fonte do visor desenha) e os caminhos das fontes; o `FontBook` (arquivos com SHA-256 conferido, reserva por caractere) não tem chamador de produção desde que os cartões saíram | puro |
| `disc_only.py` | o disco sozinho da Jukebox C: 128 px, 24 quadros de 80 ms, GIF até 256 KiB, PNG parado = quadro 0; rótulo da capa ou degradê "sem capa"; `DiscService.disc(cover, plain=)` com cache LRU por bytes (chave: sha256 do rótulo + versões) e a vaga de desenho; vaga ocupada = o plano B (a URL da capa). A capa vem da `CoverService` do processo (`utils/music/covers.cover_service()`, fechada no encerramento do pool) | um serviço por processo (`disc_service()`) |
| `cards/` | o Retrato da rádio 24 h (`radio_portrait`, com os tokens, estados e rótulos de `utils/radio/visor.py`), o disco usado pelo `disc_only` (`disc.record_image`) e as medidas e cores dele (`layout`) | puro |

Os fatos que a imagem pode mostrar (`CardSpec`) e as regras puras (call privada, animação) ficam em
`empiremuse/rules/card.py`.

## Regras que o código garante

- **Uma vaga só** para o disco da Jukebox C e os painéis da rádio 24 h, nos 5 bots. A vaga é só do desenho: busca e
  decodificação de capa ficam fora. A vaga só é devolvida quando a thread do desenho termina, mesmo que quem pediu seja
  cancelado.
- O visor da rádio passa pela mesma vaga (`panel.visor_payload_async`). Na edição, sem vaga em `SLOT_WAIT_S`, `RenderBusy`
  sobe e o painel anterior fica (quem edita registra só o tipo do erro). A rádio subindo nunca espera a vaga: ocupada, o
  painel sai sem o visor e a edição de sempre o traz.
- Um desenho acima de `SLOW_RENDER_S` vai ao log só com o rótulo e a duração.
- A chave do disco é o `sha256` dos pixels do rótulo (capa reduzida, nunca um "sem capa" genérico) mais as versões; nunca
  a URL, a faixa, o servidor ou o membro. Um disco maior que o teto do cache não é guardado e o render segue.
- Vaga sob pressão (`DEGRADE_AT` esperando) ou ocupada: o disco não é desenhado e a mensagem sai com o plano B.
- A capa e o logo da estação (rádio ao vivo na Jukebox) esperam no máximo `COVER_WAIT_S`; depois a capa sai como o
  degradê com a nota (nunca o avatar do bot) e o logo como a imagem padrão por URL, com o redesenho pedido.

## Orçamento de memória (para conferir com a RAM do host)

| Item | Teto | Onde |
|---|---|---|
| Cache dos discos da Jukebox C (GIF + PNG por rótulo, LRU) | 16 MiB (por bytes) | `disc_only.CACHE_BYTES` |
| Cache de capas (240 × 240 RGB, cerca de 48 capas) | 8 MiB (por bytes) | `utils/music/covers.py` |
| Desenho em andamento | 20 a 40 MB, **um de cada vez** (a vaga) | transitório |
| Decodificador isolado da capa | um processo de cerca de 35 a 50 MB, **um de cada vez** | transitório |
| Decodificador isolado do logo da estação | um processo como o da capa; os dois podem existir ao mesmo tempo (travas separadas, `RLIMIT_AS` de 768 MiB cada) | transitório |

Pior caso acima da base do processo: **cerca de 24 MiB de cache residente mais até cerca de 140 MB transitórios** (um
desenho e os dois decodificadores ao mesmo tempo).
