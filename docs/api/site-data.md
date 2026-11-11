# `site-data.json`: comandos públicos do bot para o site

Arquivo gerado a **cada release do bot (rc inclusive)** e anexado a ele, ao lado de `openapi.yaml`, cada um com um `.sha256`. O
EmpireMuse-Panel fixa a versão (tag e hash), baixa, confere o hash e gera `/commands`, `llms-full.txt` e o JSON-LD **só** desse arquivo.

- **Formato:** [`site-data.schema.json`](site-data.schema.json) (JSON Schema 2020-12). O painel valida o arquivo contra ele; os objetos
  são fechados (`additionalProperties: false`), então um campo novo sem versão do schema quebra a validação de propósito.
- **Versão:** `schema_version` é `1`. Dentro dela só mudança aditiva; uma quebra sobe o número e o painel recusa o que não conhece.
- **Idiomas:** `pt-BR` e `en-US` em todo texto. O `en-US` é o texto-base do Discord; o `pt-BR` vem do catálogo (`cmd.<id>.*`).
- **Permissões:** são as do usuário (`default_member_permissions` e as verificações `has_permissions` e `has_guild_permissions`, no comando e nos subcomandos). O que o bot precisa (`bot_has_*`, como `send_messages`) nunca entra.
- **Conteúdo:** comandos de barra (com opções, grupos e subcomandos), comandos de prefixo (com aliases do catálogo), categoria,
  permissões de servidor que o **usuário** precisa ter e `bot_version`. Ordem estável por `id` e nenhum campo de data: o mesmo código gera os mesmos bytes.
- **O que nunca entra:** comando de dono, comando oculto (`hidden`), segredo e id interno (como o nome da classe do cog). Um teste do bot
  falha se algum aparecer.
- **Fora do arquivo:** menus de contexto (usuário e mensagem), a regra de execução (DJ, estar no canal de voz) e qualquer sinal de
  *premium*: o catálogo não tem esse dado como campo (só aparece no texto da descrição, como na `/radio`), então o arquivo não o inventa.
  Se o site precisar dele, entra como campo aditivo quando o catálogo o tiver.

## Como gerar

```bash
python scripts/export_site_data.py --bot-version 2.3.0 --out site-data.json
```

Sem rede e sem login no Discord: lê os cogs (as classes, sem criar nenhum bot) e **só** os catálogos `en-US` e `pt-BR` de `empiremuse/locales`. O exportador instala, antes de importar qualquer cog, o `I18n` único do processo restrito a esses dois idiomas; como o `localized()` de cada cog consulta esse singleton na importação, nenhum outro diretório de idioma (como os que o Crowdin baixa) é aberto no processo inteiro. Se o processo já tem um `I18n` sem essa restrição, o exportador falha (código 2) em vez de seguir: rode-o num processo novo. O release roda o
mesmo código **dentro da imagem publicada**, sem rede (`docker run --network none`), então o arquivo descreve exatamente o código lançado.

## Conferir

```bash
sha256sum -c site-data.json.sha256
```

## Proveniência e verificação

- **`openapi.yaml`** é copiado do checkout da tag no job de release e atestado ali (`actions/attest-build-provenance`).
- **`site-data.json`** sai de **dentro da imagem publicada** (`docker run --network none --read-only --cap-drop ALL --security-opt no-new-privileges`), então descreve exatamente o código lançado, e também é atestado.
- A verificação é o **atestado**, contra a identidade do workflow `release.yml`. Os arquivos `.sha256` são só **conveniência** (conferir um download); não provam origem, porque quem troca o arquivo troca o hash.

## Se a exportação falhar depois de a imagem ser publicada

O job `docker` já publicou e assinou a imagem (o digest fica no resultado dele). Se o job `github-release` falhar (exportação, atestado ou criação da release), o caminho é **Re-run failed jobs** do mesmo workflow: ele repete só o que falhou e reaproveita o digest. **Nunca mover nem recriar a tag**: o workflow recusa reconstruir uma versão já publicada.
