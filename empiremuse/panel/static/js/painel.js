// Configuração do htmx e proteção XSRF do painel EmpireMuse.
// Sem eval, sem script inline: a CSP do painel só permite scripts do próprio domínio.
(function () {
  "use strict";

  var XSRF_HEADER = "X-XSRFToken";
  var MENSAGEM_ERRO = "Não foi possível concluir a ação. Tente novamente em instantes.";
  var MENSAGEM_REDE = "Sem conexão com o painel. Verifique sua internet e tente novamente.";

  if (!window.htmx) {
    return;
  }

  // Reforço: o <meta name="htmx-config"> já aplica estes valores antes do htmx iniciar.
  window.htmx.config.allowEval = false;
  window.htmx.config.allowScriptTags = false;
  window.htmx.config.selfRequestsOnly = true;
  window.htmx.config.includeIndicatorStyles = false;

  // 409 (conflito), 422 (validação) e 429 (limite) devolvem o formulário com o estado atual: o htmx deve trocá-lo.
  // Os demais 4xx/5xx (inclusive 413, que devolve só um aviso) não trocam o conteúdo;
  // o aviso é mostrado em #avisos para o formulário continuar na tela.
  window.htmx.config.responseHandling = [
    { code: "204", swap: false },
    { code: "[23]..", swap: true },
    { code: "409|422|429", swap: true, error: false },
    { code: "[45]..", swap: false, error: true },
    { code: "...", swap: true }
  ];

  function lerTokenXsrf() {
    var meta = document.querySelector('meta[name="xsrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function mostrarAviso(texto) {
    var area = document.getElementById("avisos");
    if (!area) {
      return;
    }
    var aviso = document.createElement("p");
    aviso.className = "aviso aviso-erro";
    aviso.textContent = texto;
    area.replaceChildren(aviso);
  }

  document.addEventListener("htmx:configRequest", function (evento) {
    var token = lerTokenXsrf();
    if (token) {
      evento.detail.headers[XSRF_HEADER] = token;
    }
  });

  // Texto do aviso devolvido pelo servidor (sem inserir HTML: só textContent).
  function textoDoAviso(xhr) {
    try {
      var doc = new DOMParser().parseFromString(xhr.responseText || "", "text/html");
      var aviso = doc.querySelector(".aviso");
      var texto = aviso ? aviso.textContent.trim() : "";
      return texto.length > 0 && texto.length <= 300 ? texto : "";
    } catch (erro) {
      return "";
    }
  }

  document.addEventListener("htmx:responseError", function (evento) {
    var xhr = evento.detail && evento.detail.xhr;
    mostrarAviso((xhr && textoDoAviso(xhr)) || MENSAGEM_ERRO);
  });

  // Depois de trocar um formulário, leva o foco para a mensagem ou para o primeiro campo inválido.
  document.addEventListener("htmx:afterSettle", function (evento) {
    var origem = evento.detail && evento.detail.requestConfig && evento.detail.requestConfig.elt;
    if (!origem || !origem.id) {
      return;
    }
    // Acompanhamento de tarefa: ao chegar ao estado final, leva o foco para o resultado (uma única vez).
    if (origem.id === "tarefa-jukebox") {
      var bloco = document.getElementById("tarefa-jukebox");
      var estado = bloco ? bloco.getAttribute("data-estado") : "";
      if (bloco && (estado === "ok" || estado === "erro")) {
        bloco.focus();
      }
      return;
    }
    if (origem.tagName !== "FORM") {
      return;
    }
    var INVALIDO = '[aria-invalid="true"]';
    var MENSAGEM = ".mensagem-form";
    var formulario = document.getElementById(origem.id);
    var escopo = formulario || document.getElementById("conteudo");
    var alvo = escopo && (escopo.querySelector(INVALIDO) || escopo.querySelector(MENSAGEM));
    if (!alvo) {
      var conteudo = document.getElementById("conteudo");
      alvo = conteudo && (conteudo.querySelector(INVALIDO) || conteudo.querySelector(MENSAGEM) || conteudo.querySelector("#tarefa-canal"));
    }
    if (alvo) {
      alvo.focus();
    }
    var area = document.getElementById("avisos");
    if (area) {
      area.replaceChildren();
    }
  });

  document.addEventListener("htmx:sendError", function () {
    mostrarAviso(MENSAGEM_REDE);
  });
})();
