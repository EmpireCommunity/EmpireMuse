# Atualização Automática (comando `update`)

O dono atualiza o bot pelo comando `update` (prefixo ou botão "Atualizar Bot" do painel). Só o dono aciona e confirma (60 s), e só um `update` roda por vez. A confirmação mostra a versão atual e a nova, o "o que mudou" do `CHANGELOG.md` e o aviso de que o bot fica indisponível durante a troca. Quando o bot volta, ele conta o resultado no canal de origem (ou por DM, sem menções).

---

## 1. Modos

O modo é detectado na subida:

| Modo | Quando | O que o `update` faz |
| :--- | :--- | :--- |
| `docker` | `UPDATE_PROXY_SOCKET` configurado (seção 2) | Troca a imagem do bot sozinho, pelo auxiliar do proxy |
| `panel` | Imagem Docker sem o proxy | Mostra a versão disponível e manda atualizar pelo painel da hospedagem |
| `git` | Clone git (`.git` na pasta do projeto) | Atualiza por tag anotada e reinicia (seção 4) |
| `manual` | Código solto, sem clone | Mostra a versão disponível e manda reimplantar |

Nos modos `panel` e `manual`, se a consulta da versão falhar, o bot responde "Não consegui consultar a versão disponível agora." com a ação do modo e o link das versões publicadas. Discloud, Pterodactyl e o `deploy/docker-compose.yml` base não têm o proxy: caem em `panel` ou `manual`.

---

## 2. Como Funciona no Docker

```text
dono → bot → proxy (socket Unix) → docker.sock → auxiliar → compose up
```

1. O bot consulta as releases do GitHub e só considera as **definitivas e imutáveis**, com tag `vX.Y.Z` e o asset `image-digest.txt` (o digest da imagem publicada, assinada no `release.yml`; veja [`release.md`](release.md), seções 4.3 e 5.3). O alvo é a maior versão acima da instalada.
2. Depois da confirmação, o bot pede ao proxy um container auxiliar para `X.Y.Z@sha256:<digest>`. O bot **nunca** recebe o `docker.sock` nem o grupo do Docker: só fala com o socket do proxy.
3. O proxy aceita 5 operações fixas e cria o auxiliar a partir de um modelo fixo (imagem, comando e limites vêm do código do proxy, nunca do bot).
4. O auxiliar não tem rede e só fala com o daemon pelo `docker.sock` (equivale a root no host, por desenho; é o daemon que baixa a imagem). Ele confere que a imagem baixada declara a versão alvo, recusa downgrade, guarda um backup do compose, troca **uma única linha** `image:` do bot e sobe o serviço com `compose up --wait`. Se falhar, restaura o compose anterior.
5. O bot novo sobe, confere a versão que de fato subiu e o resultado do auxiliar e avisa uma vez.

**Versão de teste.** Uma imagem de teste (`X.Y.Z-rc.N`, a `rc` da rehearsal da release) reconhece a própria versão e pode usar o `/update` para a próxima versão **estável** (`2.1.0-rc.2` → `2.1.0`). Só a versão **atual** pode ser de teste: o alvo é sempre estável, nunca um `rc`. Sem estável mais nova, o bot responde "✅ **Você está na versão de teste X.Y.Z-rc.N; ainda não há versão estável mais nova.**". Na imagem em modo `docker` e no modo `panel`, a linha `image:` do compose pode estar numa `X.Y.Z-rc.N@sha256:…`; o auxiliar troca essa linha pela estável e o código 67 continua valendo (alvo não é maior que a versão atual).

O proxy é o arquivo único `empiremuse/updater/proxy.py`, só biblioteca padrão, rodado com `python -I -S`. A decisão e as alternativas estão no [ADR-0004](../adr/0004-auto-update-proxy.md).

### 2.1 Implantação passo a passo (compose da Hostinger)

Pré-requisito: o repositório com **Releases imutáveis** ligadas e uma release com `image-digest.txt` (seção 5.4 do [`release.md`](release.md)). Use o [`deploy/docker-compose.hostinger.yml`](../../deploy/docker-compose.hostinger.yml) (ou o [`deploy/docker-compose.hostinger-sem-traefik.yml`](../../deploy/docker-compose.hostinger-sem-traefik.yml), se não usa Traefik; o proxy de atualização é o mesmo) e o [`deploy/env.example.hostinger`](../../deploy/env.example.hostinger). O [índice do `deploy/`](../../deploy/README.md) mostra quais tipos de implantação têm auto-update.

1. **Digest das duas imagens.** O compose traz dois marcadores de zeros (`@sha256:000…`): a imagem do `bot` (`or4cu1o/empiremuse:X.Y.Z@sha256:…`) e a do `updater-proxy` (`docker.io/or4cu1o/empiremuse:X.Y.Z@sha256:…`). Troque os **dois** pelo conteúdo do `image-digest.txt` da mesma versão:

   ```bash
   gh release download vX.Y.Z --repo EmpireCommunity/EmpireMuse -p image-digest.txt -O -
   ```

   Na primeira vez, confira a assinatura desse digest com o `cosign verify` da seção 5.3 do [`release.md`](release.md) antes de fixá-lo.

   Mantenha a linha do bot na forma exata, com 4 espaços (`    image: or4cu1o/empiremuse:X.Y.Z@sha256:…`): é a que o auxiliar edita. O proxy usa o nome completo (`docker.io/or4cu1o/empiremuse:X.Y.Z@sha256:…`) para nunca casar com essa linha; se a do bot sair dessa forma, todo `update` falha com o código 65.
2. **`DOCKER_GID` no `.env`.** Descubra o grupo dono do socket e preencha a variável (obrigatória enquanto o `updater-proxy` existir; só o proxy recebe esse grupo):

   ```bash
   stat -c %g /var/run/docker.sock
   ```

3. **Caminho do projeto.** Em `updater-proxy`, `UPDATER_PROJECT_DIR` tem de ser o caminho **deste projeto no host** (o auxiliar monta a pasta nele para editar o compose). `UPDATER_PEER_UID` (`10001`, o usuário do bot) restringe quem fala com o proxy.
4. **Permissões (exigência do código 65).** A pasta do projeto, o `docker-compose.yml` e o `.env` precisam ser de `root`, sem link simbólico e sem escrita para grupo ou outros:

   ```bash
   chown root:root <pasta> <pasta>/docker-compose.yml <pasta>/.env
   chmod go-w <pasta> <pasta>/docker-compose.yml
   chmod 600 <pasta>/.env
   ```

   O `chmod 600` no `.env` também impede que outros usuários do host leiam os segredos (a checagem do auxiliar só barra escrita).

   Vale o mesmo, por boa prática, para as pastas-pai e para arquivos que o compose cita (como `env_file`). Quem consegue gravar o compose ou o `.env` decide o que o auxiliar sobe como root.
5. **Conferir antes.** `docker compose -f docker-compose.yml config -q` com o `.env` preenchido, e o `docker inspect empiremuse` (container do bot) em um projeto já implantado: o label `com.docker.compose.project` tem de ser `empiremuse` e o `com.docker.compose.project.working_dir` tem de ser igual ao `UPDATER_PROJECT_DIR`. Se diferir, o auxiliar não enxerga o mesmo projeto.
6. **Implantar e conferir depois.** Reimplante o projeto pelo painel. No container do proxy, `/run/empiremuse-updater` deve ser `10002:10001` modo `0750` e o `updater.sock` modo `0660`. Se um volume `updater-sock` antigo subiu com dono `root`, remova esse volume e reimplante.
7. **Sem o auto-update**, apague o serviço `updater-proxy`, o volume `updater-sock` e, no bot, `UPDATE_PROXY_SOCKET` e o volume.

### 2.2 Códigos de saída do auxiliar

| Código | Significado | Efeito |
| :--- | :--- | :--- |
| 0 | Atualização concluída | Bot na versão nova |
| 64 | Versão alvo inválida | Nada mudou |
| 65 | Compose fora do formato esperado (linha `image:`, backup, arquivo temporário ou troca do arquivo) ou permissões inseguras (dono, modo, link simbólico) | Nada mudou |
| 66 | Falha ao baixar a imagem | Nada mudou |
| 67 | A versão alvo não é maior que a instalada | Nada mudou |
| 68 | A imagem baixada não declara a versão anunciada | Nada mudou |
| 70 | A atualização falhou e o compose voltou à versão anterior | Bot na versão anterior |
| 71 | Falhou e a volta também falhou | O bot provavelmente está fora do ar, sem aviso no Discord. Confira o servidor; os backups ficam em `<pasta>/docker-compose.yml.bak-AAAAMMDD-HHMMSS`, para reverter à mão |

No canal sai só o significado do código. As últimas linhas do log do auxiliar (sem segredos) vão por DM a quem pediu.

---

## 3. Verificação da Imagem

O bot só oferece versões com GitHub Release imutável e o auxiliar puxa **por digest**, então um token vazado do Docker Hub não escolhe o que roda. O bot **não** roda `cosign verify` na hora do `update`; para conferir a assinatura como pessoa, use o comando da seção 5.3 do [`release.md`](release.md). Quem tem escrita no repositório pode trocar o asset de uma release que ainda não é imutável.

---

## 4. Modo Git (clone, fora do Docker)

O `update` aceita só tags anotadas `vX.Y.Z` mais novas que a versão atual e contidas na `main`, do repositório oficial. Roda `git` sem shell, instala as dependências com hashes e faz um teste de importação. Se algo falhar, volta ao commit anterior e reinstala o lock anterior. Ao aplicar, o bot **sai com o código 75**, e o supervisor o reinicia.

Sem supervisor (`INVOCATION_ID` ausente, ou seja, sem systemd), o bot avisa na confirmação que vai parar e precisa ser iniciado à mão. Exemplo de unit (ajuste usuário e caminhos):

```ini
[Service]
User=empiremuse
WorkingDirectory=/caminho/EmpireMuse
ExecStart=/caminho/EmpireMuse/.venv/bin/python -m empiremuse
Restart=on-failure
RestartForceExitStatus=75
```

A guarda de boot (`boot_guard`) roda antes de importar o bot: se a versão nova não deixar o pool pronto em 2 subidas, na terceira ela volta ao commit anterior, reinstala o lock anterior e sai com 75. Além disso:

- uma atualização interrompida no meio (estado `applying`) é revertida já na próxima subida, sem esperar 3 subidas;
- se o estado não puder ser gravado (disco cheio ou somente leitura), o bot sobe como está, sem reverter, com um erro no log.

---

## 5. Limitações

- **Lavalink fora do ar (R5).** O sucesso da subida exige um nó de áudio quando há nós esperados. Uma atualização boa, com o Lavalink fora do ar e 2 reinícios em 24 h, causa uma reversão: o aviso diz "falhou e voltou" e o dono repete o `update`. Confira o Lavalink antes de atualizar ou de repetir.
- **Modo git.**
  - Se um arquivo seu (ou link) ocupa um caminho que a nova versão acrescenta, o `update` é recusado de antemão, com até 5 caminhos listados. A reversão só apaga arquivos idênticos aos da nova versão e mantém os seus.
  - Edições em arquivos rastreados feitas entre um `update` e a reversão dele são descartadas (`checkout --force`); o `update` em si recusa uma árvore rastreada suja.
  - Não rode `git` à mão no clone durante um `update` ou uma reversão: pode colidir com ela. Depois de 3 tentativas de reversão sem sucesso, o bot desiste com aviso.
- **Painel da Hostinger.** O painel e o auxiliar editam o mesmo compose, então não edite o compose pelo painel durante um `update`. A tolerância do painel a um compose alterado por fora é validada no primeiro `update` real: depois dele, confira no editor YAML do painel se a versão nova aparece. O "Update project" do painel puxa e recria **todos** os serviços, por isso as duas linhas de imagem usam digest.
- **Imagens antigas (R2).** Cada atualização deixa a imagem anterior no host e não há limpeza automática; rode `docker image prune` de tempos em tempos (confira antes o que ele remove).
- **Atualização Docker travada (D12).** Se o auxiliar terminou depois de o bot desistir de acompanhá-lo (15 min), o estado pendente fica e o próximo `update` o resolve.
- **Imagem do proxy.** O `update` só altera a linha do bot, nunca a do `updater-proxy`. Uma correção no proxy só chega à produção quando o dono troca o digest do proxy à mão no compose (por exemplo, sempre que o `CHANGELOG.md` citar o proxy), conferindo antes a assinatura (seção 5.3 do [`release.md`](release.md)).
- **Superfície.** O `docker.sock` equivale a root no host: por isso só o proxy e o auxiliar o veem, o proxy é mínimo e o bot é tratado como possivelmente comprometido.
