/*
 * Atualiza o andamento do lote sem recarregar a pagina.
 *
 * Arquivo externo por causa da CSP estrita (script-src 'self'). A URL do
 * endpoint e o estado inicial chegam por atributos data-* no container.
 */
(function () {
  "use strict";

  var raiz = document.getElementById("painel-progresso");
  if (!raiz || raiz.dataset.finalizado === "true") { return; }

  var URL_PROGRESSO = raiz.dataset.urlProgresso;
  var lista   = document.getElementById("lista-arquivos");
  var barra   = document.getElementById("barra");
  var preench = document.getElementById("barra-preenchida");
  var texto   = document.getElementById("texto-progresso");
  var selo    = document.getElementById("selo-status");
  var dica    = document.getElementById("dica-atualizacao");

  var CLASSE_SELO = {
    concluido: "badge-green",
    processando: "badge-blue",
    erro: "badge-red",
    recusado: "badge-yellow",
    pendente: "badge-cinza"
  };
  var TEXTO_SELO = {
    concluido: "Concluido",
    processando: "Processando",
    erro: "Erro",
    recusado: "Recusado",
    pendente: "Na fila"
  };

  var falhasSeguidas = 0;
  var intervalo = 2000;

  dica.textContent = "Atualizando automaticamente...";

  function desenharArquivo(arquivo) {
    var linha = document.createElement("div");
    linha.className = "linha-arquivo";
    linha.setAttribute("data-estado", arquivo.status);

    var nome = document.createElement("span");
    nome.className = "linha-arquivo-nome";
    nome.textContent = arquivo.nome;
    linha.appendChild(nome);

    if (arquivo.tamanho_kb) {
      var meta = document.createElement("span");
      meta.className = "linha-arquivo-meta";
      meta.textContent = arquivo.tamanho_kb + " KB" +
        (arquivo.paginas ? " · " + arquivo.paginas + "p" : "");
      linha.appendChild(meta);
    }

    var badge = document.createElement("span");
    badge.className = "badge " + (CLASSE_SELO[arquivo.status] || "badge-cinza");
    if (arquivo.status === "processando") {
      var girando = document.createElement("span");
      girando.className = "girando";
      girando.setAttribute("aria-hidden", "true");
      badge.appendChild(girando);
      badge.appendChild(document.createTextNode(" "));
    }
    badge.appendChild(document.createTextNode(TEXTO_SELO[arquivo.status] || arquivo.status_rotulo));
    linha.appendChild(badge);

    if (arquivo.mensagem) {
      var motivo = document.createElement("span");
      motivo.className = "linha-arquivo-motivo";
      // textContent, nunca innerHTML: a mensagem carrega o nome do arquivo
      // enviado, que e conteudo controlado por quem faz o upload.
      motivo.textContent = arquivo.mensagem;
      linha.appendChild(motivo);
    }

    return linha;
  }

  function consultar() {
    fetch(URL_PROGRESSO, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      })
      .then(function (dados) {
        falhasSeguidas = 0;

        preench.style.width = dados.percentual + "%";
        barra.setAttribute("aria-valuenow", dados.percentual);
        texto.textContent = dados.concluidos + " de " + dados.total + " arquivo(s)";
        selo.textContent = dados.status_rotulo;
        selo.className = "badge " + (CLASSE_SELO[dados.status] || "badge-cinza");

        lista.innerHTML = "";
        dados.arquivos.forEach(function (arquivo) {
          lista.appendChild(desenharArquivo(arquivo));
        });

        if (dados.finalizado) {
          dica.textContent = "Processamento concluido.";
          return;
        }
        setTimeout(consultar, intervalo);
      })
      .catch(function () {
        falhasSeguidas += 1;
        if (falhasSeguidas >= 5) {
          dica.textContent = "Nao foi possivel atualizar. Recarregue a pagina.";
          return;
        }
        // Espaca as tentativas quando o servidor nao responde, em vez de
        // insistir no mesmo ritmo.
        intervalo = Math.min(intervalo * 2, 30000);
        setTimeout(consultar, intervalo);
      });
  }

  setTimeout(consultar, intervalo);
})();
