# Fontes

Arquivos de fonte que o bot usa para desenhar imagens. Nada é baixado em execução: tudo entra no repositório, com a origem e o hash abaixo.

## Skins de imagem (2.3)

Baixadas do repositório oficial [`google/fonts`](https://github.com/google/fonts) no commit fixo
[`06bfe0d62a98375cb09c942fa18e68bd32cff057`](https://github.com/google/fonts/tree/06bfe0d62a98375cb09c942fa18e68bd32cff057)
(2026-11-20). Cada arquivo foi conferido contra o blob do commit (`git hash-object`). O `empiremuse/utils/render/fonts.py`
confere o SHA-256 na primeira carga e recusa um arquivo diferente (`MANIFEST`).

| Arquivo | Origem no commit | Versão | Bytes | SHA-256 |
|---|---|---|---|---|
| `Unbounded[wght].ttf` | `ofl/unbounded/Unbounded[wght].ttf` | 1.701 | 778.272 | `323b511be380c8d474ef030686b71aedde501f8d9cd46da558b7c40454372c3f` |
| `Figtree[wght].ttf` | `ofl/figtree/Figtree[wght].ttf` | 2.002 | 62.712 | `26ad3db9b31ff7dde67a91ff515d022d2f495cd506590699cf264f0bfe6fb714` |
| `NotoSans[wdth,wght].ttf` | `ofl/notosans/NotoSans[wdth,wght].ttf` | 2.015 | 2.049.096 | `bfb7bb691513f12e734dc346c03a03f784912432d7e3fa8e56efcf906fe86b3d` |
| `LICENSE-Unbounded.txt` | `ofl/unbounded/OFL.txt` | | 4.392 | `31e5d4e83955e7103c34570dd49b0570ef490800bd65b42923c0dd02445263b3` |
| `LICENSE-Figtree.txt` | `ofl/figtree/OFL.txt` | | 4.388 | `140d37233e7f3ce7313798befa9600893bcceaf41a55fa0fa5ad52f7f657a268` |
| `LICENSE-NotoSans.txt` | `ofl/notosans/OFL.txt` | | 4.396 | `cee9892f9f0cc8fe882c9e9537ee6a89621d86ee7ceaf70b02e2b2b1c25c061a` |

Total: 2.903.256 bytes (cerca de 2,8 MiB) a mais na imagem Docker.

- As três são fontes variáveis sob a SIL Open Font License 1.1. O peso vai pelo eixo `Weight` (`set_variation_by_axes`,
  montado pelo nome do eixo); a Noto Sans tem também o eixo `Width`, que fica no padrão.
- **Unbounded:** títulos. Latim, grego e cirílico.
- **Figtree:** texto. Só latim.
- **Noto Sans:** reserva por caractere quando a fonte da skin não tem o glifo (latim estendido, grego e cirílico).
- Fora desta versão: Noto CJK, árabe, hebraico, tailandês e devanágari. Texto sem glifo sai da imagem, e o título real vai
  no embed. Texto da direita para a esquerda só é desenhado com o `raqm` disponível no Pillow (`features.check("raqm")`).

### Atualizar

1. Escolher um commit do `google/fonts` e baixar os mesmos caminhos de `raw.githubusercontent.com/google/fonts/<commit>/...`.
2. Conferir cada arquivo contra o blob do commit (`gh api repos/google/fonts/contents/<caminho>?ref=<commit>` dá o `sha`
   de `git hash-object`).
3. Atualizar esta tabela e o `MANIFEST` de `empiremuse/utils/render/fonts.py` no mesmo commit.

## Visor do painel de rádio

`Inter-{Regular,Medium,SemiBold,Bold}.ttf` e `LICENSE-Inter.txt` (The Inter Project Authors, SIL Open Font License 1.1): ver
`CREDITS.md`.
