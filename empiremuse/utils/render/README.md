# `utils/render/`: pipeline de imagem das skins

Base do render das skins de imagem da 2.3 (Jukebox e rádio 24 h). O contrato de implementação é privado; este README
resume o que o código garante e o orçamento de memória.

## Módulos

| Módulo | Faz | Estado |
|---|---|---|
| `slot.py` | `RenderSlot`: uma vaga de desenho (`asyncio.Semaphore(1)`) com contador de espera; `run(fn, *args)` numa thread; `pressure()`; `RenderBusy` quando a espera passa de `SLOT_WAIT_S` | uma por processo (`shared_slot()`) |
| `budget.py` | todos os tetos nomeados (os de regra pura vêm de `rules/card.py` e são reexportados) | constantes |
| `frames.py` | `DiscFrames` (paleta + ladrilhos `P` crus) e `FramesCache`, um LRU limitado por **bytes** e sem trava (só no laço de eventos); `frames_key` | cache por bytes |
| `gif.py` | paleta única por faixa, quantização dos ladrilhos, GIF (`disposal=1`, `optimize=False`, 30 × 60 ms, laço infinito) e PNG (`compress_level=6`) | puro |
| `primitives.py` | ícones, logo em círculo (só PNG), `fit` por busca binária, texto recortado na caixa | puro |
| `fonts.py` | `FontBook`: só os arquivos de `assets/fonts/` com o SHA-256 registrado, conferidos na primeira carga; toda fonte (peso e tamanho) sai desses bytes conferidos em memória; peso pelo eixo `Weight`; reserva por caractere (a fonte da skin, depois a Noto Sans, até 32 trechos); RTL só com `raqm` | cache por `(arquivo, peso, tamanho)`; usar dentro da vaga |
| `state.py` | estado de render por player num `WeakKeyDictionary` (último anexo, GIF adiado, último GIF frio), fora de `player.py` | memória |

Os fatos que a imagem pode mostrar (`CardSpec`) e as regras puras (formato, animação, anexo, call privada) ficam em
`empiremuse/rules/card.py`.

## Regras que o código garante

- **Uma vaga só** para os players dos 5 bots e o visor da rádio 24 h. A vaga é só do desenho: busca e decodificação de
  capa ficam fora. A vaga só é devolvida quando a thread do desenho termina, mesmo que quem pediu seja cancelado.
- O visor da rádio passa pela mesma vaga (`panel.visor_payload_async`). Na edição, sem vaga em `SLOT_WAIT_S`, `RenderBusy`
  sobe e o painel anterior fica (quem edita registra só o tipo do erro). A rádio subindo nunca espera a vaga: ocupada, o
  painel sai sem o visor e a edição de sempre o traz.
- Um desenho acima de `SLOW_RENDER_S` vai ao log só com o rótulo (`visor` ou o id da skin) e a duração.
- O cache de quadros nunca passa do teto em bytes; uma entrada maior que o teto não é guardada e o render segue.
- A chave dos quadros é o `sha256` dos pixels do rótulo do disco (capa reduzida ou logo, nunca um "sem capa" genérico) mais skin, escala, tema, quadros e `RENDER_VERSION`;
  nunca a URL, a faixa, o servidor ou o membro.
- Manter o anexo de uma mensagem só com a chave de render **igual** à anterior (igualdade de tupla, nunca o `hash`).

## Orçamento de memória (para conferir com a RAM do host)

| Item | Teto | Onde |
|---|---|---|
| Cache de quadros do disco | 24 MiB (por bytes) | `budget.FRAMES_CACHE_BYTES` |
| Cache de capas (240 × 240 RGB, cerca de 48 capas) | 8 MiB (por bytes) | módulo da capa (PR da capa) |
| Fontes (bytes conferidos, uma cópia por arquivo, mais as instâncias por peso e tamanho) | cerca de 3 MiB | `fonts.FontBook` |
| Desenho em andamento | 20 a 40 MB em 1×, **um de cada vez** (a vaga) | transitório |
| Decodificador isolado da capa | um processo de cerca de 35 a 50 MB, **um de cada vez** | transitório |

Pior caso acima da base do processo: **cerca de 32 MiB de cache residente mais até cerca de 90 MB transitórios**
(um desenho e um decodificador ao mesmo tempo), perto de 120 MB no pico. Medidas da bancada (Pillow 13.0.0, host de
produção): Expandida 480 × 840 com GIF frio em 140 ms e 0,79 MB, com cache em 35 ms; PNG em 40 ms e 124 KiB.
