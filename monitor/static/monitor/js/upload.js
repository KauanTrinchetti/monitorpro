/*
 * Pre-visualizacao e conferencia dos PDFs antes do envio.
 *
 * Arquivo externo, e nao <script> inline, porque a CSP da aplicacao usa
 * script-src 'self' — sem 'unsafe-inline'. Manter a politica estrita e o que
 * impede que um script injetado em qualquer campo da pagina execute.
 * A configuracao que antes vinha interpolada pelo template agora chega por
 * atributos data-* no formulario.
 */
(function () {
  "use strict";

  var formul = document.getElementById("form-envio");
  if (!formul) { return; }

  var campo  = document.getElementById(formul.dataset.campoId);
  var zona   = document.getElementById("zona-envio");
  var previa = document.getElementById("previa-arquivos");
  var botao  = document.getElementById("botao-enviar");
  var rotulo = document.getElementById("rotulo-enviar");

  if (!campo) { return; }

  var LIMITE_ARQUIVOS = parseInt(formul.dataset.limiteArquivos, 10) || 20;
  var LIMITE_MB       = parseInt(formul.dataset.limiteMb, 10) || 25;
  var LIMITE_BYTES    = LIMITE_MB * 1024 * 1024;

  function formatarTamanho(bytes) {
    if (bytes < 1024) { return bytes + " B"; }
    if (bytes < 1048576) { return (bytes / 1024).toFixed(1) + " KB"; }
    return (bytes / 1048576).toFixed(1) + " MB";
  }

  // Conferencia local serve so para retorno imediato. A validacao que vale e
  // a do servidor (forms.UploadFaturasForm + seguranca.validar_pdf), que roda
  // de novo em tudo — inclusive num POST que nao passe por esta tela.
  function conferir(arquivo) {
    if (arquivo.size === 0) { return "Arquivo vazio."; }
    if (arquivo.size > LIMITE_BYTES) {
      return "Excede " + LIMITE_MB + " MB (" + formatarTamanho(arquivo.size) + ").";
    }
    if (!/\.pdf$/i.test(arquivo.name)) { return "Extensao nao e .pdf."; }
    return null;
  }

  function criarLinha(arquivo, problema) {
    var linha = document.createElement("div");
    linha.className = "linha-arquivo";
    linha.setAttribute("data-estado", problema ? "recusado" : "pendente");

    var nome = document.createElement("span");
    nome.className = "linha-arquivo-nome";
    nome.textContent = arquivo.name;
    linha.appendChild(nome);

    var meta = document.createElement("span");
    meta.className = "linha-arquivo-meta";
    meta.textContent = formatarTamanho(arquivo.size);
    linha.appendChild(meta);

    var selo = document.createElement("span");
    selo.className = "badge " + (problema ? "badge-yellow" : "badge-cinza");
    selo.textContent = problema ? "Nao sera enviado" : "Pronto";
    linha.appendChild(selo);

    if (problema) {
      var motivo = document.createElement("span");
      motivo.className = "linha-arquivo-motivo";
      motivo.textContent = problema;
      linha.appendChild(motivo);
    }

    return linha;
  }

  function desenhar() {
    var arquivos = Array.prototype.slice.call(campo.files || []);
    previa.innerHTML = "";

    if (!arquivos.length) {
      botao.disabled = true;
      botao.setAttribute("aria-disabled", "true");
      rotulo.textContent = "Selecione ao menos um arquivo";
      return;
    }

    var validos = 0;
    arquivos.forEach(function (arquivo) {
      var problema = conferir(arquivo);
      if (!problema) { validos += 1; }
      previa.appendChild(criarLinha(arquivo, problema));
    });

    if (arquivos.length > LIMITE_ARQUIVOS) {
      var aviso = document.createElement("div");
      aviso.className = "message warning";
      aviso.setAttribute("role", "alert");
      aviso.textContent = "Voce selecionou " + arquivos.length +
        " arquivos. O limite e " + LIMITE_ARQUIVOS + " por envio.";
      previa.insertBefore(aviso, previa.firstChild);
    }

    var podeEnviar = validos > 0 && arquivos.length <= LIMITE_ARQUIVOS;
    botao.disabled = !podeEnviar;
    botao.setAttribute("aria-disabled", String(!podeEnviar));
    rotulo.textContent = podeEnviar
      ? "Enviar " + validos + " arquivo(s)"
      : "Corrija os arquivos marcados";
  }

  campo.addEventListener("change", desenhar);

  // Arrastar e soltar e um atalho. O clique no rotulo continua funcionando,
  // entao quem navega so por teclado nao depende disto.
  ["dragenter", "dragover"].forEach(function (evento) {
    zona.addEventListener(evento, function (e) {
      e.preventDefault();
      zona.classList.add("arrastando");
    });
  });

  ["dragleave", "drop"].forEach(function (evento) {
    zona.addEventListener(evento, function (e) {
      e.preventDefault();
      zona.classList.remove("arrastando");
    });
  });

  zona.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files.length) {
      campo.files = e.dataTransfer.files;
      desenhar();
    }
  });

  // Estado de carregamento: impede duplo envio e deixa claro que os arquivos
  // estao subindo.
  formul.addEventListener("submit", function () {
    botao.disabled = true;
    botao.setAttribute("aria-disabled", "true");
    rotulo.textContent = "Enviando...";
    var girando = document.createElement("span");
    girando.className = "girando";
    girando.setAttribute("aria-hidden", "true");
    botao.insertBefore(girando, botao.firstChild);
  });
})();
