"use strict";

/* ================= utilidades ================= */

const ROTULOS = {
  rodando: "No ar",
  iniciando: "Iniciando",
  ocupado: "Em andamento",
  instalado: "Instalado",
  parado: "Parado",
  nao_instalado: "Não instalado",
  com_problema: "Com problema",
  indisponivel: "Docker indisponível",
};

const ACOES_POR_ESTADO = {
  nao_instalado: [["iniciar", "Instalar e iniciar", "primario"], ["instalar", "Só instalar"]],
  instalado: [["iniciar", "Iniciar", "primario"], ["reconstruir", "Atualizar e iniciar"]],
  parado: [["iniciar", "Iniciar", "primario"], ["reconstruir", "Atualizar e iniciar"], ["remover", "Remover container", "perigo"]],
  rodando: [["abrir", "Abrir", "primario"], ["iniciar", "Reiniciar"], ["parar", "Parar"], ["reconstruir", "Atualizar"]],
  iniciando: [["abrir", "Abrir"], ["parar", "Parar"]],
  com_problema: [["iniciar", "Reiniciar", "primario"], ["parar", "Parar"], ["reconstruir", "Atualizar e iniciar"]],
  ocupado: [],
  indisponivel: [],
};

const $conteudo = document.getElementById("conteudo");
let eu = null;
let timer = null;
// Atualização automática: rápida só enquanto algo está em andamento (build, container subindo).
let ritmo = null;          // [ms em andamento, ms parado]
let emAndamento = false;
// Sistema que a pessoa tentou abrir antes de fazer login (vem do proxy em #/login?voltar=...).
let voltarPara = "";
const EM_ANDAMENTO = new Set(["ocupado", "iniciando"]);
let rota = "";

/** Cria elementos: h("a", {href: "#", class: "x", onclick: fn}, "texto", outroEl) */
function h(tag, attrs = {}, ...filhos) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const f of filhos.flat()) {
    if (f === null || f === undefined || f === false) continue;
    el.append(f instanceof Node ? f : document.createTextNode(String(f)));
  }
  return el;
}

class ErroApi extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

async function api(caminho, opcoes = {}) {
  const resp = await fetch(caminho, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opcoes,
    body: opcoes.body !== undefined ? JSON.stringify(opcoes.body) : undefined,
  });
  const dados = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const msg = typeof dados.detail === "string" ? dados.detail : `Erro ${resp.status}`;
    if (resp.status === 401 && !caminho.startsWith("/api/auth/")) {
      eu = null;
      location.hash = "#/login";
    } else if (resp.status === 403 && msg.startsWith("Troque")) {
      location.hash = "#/senha";
    }
    throw new ErroApi(msg, resp.status);
  }
  return dados;
}

function toast(msg) {
  document.querySelector(".toast")?.remove();
  const el = h("div", { class: "toast", role: "status" }, msg);
  document.body.append(el);
  setTimeout(() => el.remove(), 5000);
}

function pilula(status) {
  return h("span", { class: "pilula", "data-estado": status.estado }, ROTULOS[status.estado] || status.estado);
}

/** Logo do sistema ou, sem logo, um monograma nas cores da marca. */
function logoSistema(s, grande = false) {
  const cls = `logo-sistema${grande ? " grande" : ""}`;
  if (s.logo) {
    return h("span", { class: cls }, h("img", { src: s.logo, alt: "", loading: "lazy" }));
  }
  const palavras = s.nome.replace(/\(.*?\)/g, "").split(/\s+/).filter((p) => p.length > 2);
  const iniciais = (palavras.length > 1 ? palavras[0][0] + palavras[palavras.length - 1][0] : s.nome.slice(0, 2)).toUpperCase();
  return h("span", { class: `${cls} monograma`, "aria-hidden": "true" }, iniciais);
}

function quando(ts) {
  if (!ts) return "Nunca";
  return new Date(ts * 1000).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

function painelSenha(titulo, senha, extra) {
  return h("div", { class: "senha-temp", role: "alert" },
    h("strong", {}, titulo),
    h("div", { class: "senha-linha" },
      h("code", {}, senha),
      h("button", {
        type: "button", class: "botao",
        onclick: async (ev) => {
          try { await navigator.clipboard.writeText(senha); ev.target.textContent = "Copiada"; }
          catch { toast("Não foi possível copiar; selecione e copie manualmente."); }
        },
      }, "Copiar")),
    h("small", {}, extra || "Anote agora: ela não será mostrada de novo. No primeiro acesso a pessoa define uma senha própria."));
}

/* ================= cabeçalho ================= */

function montarTopo() {
  const topo = document.getElementById("topo");
  const menu = document.getElementById("menu");
  const conta = document.getElementById("conta");
  if (!eu || rota === "#/login") {
    topo.hidden = true;
    return;
  }
  topo.hidden = false;
  menu.replaceChildren();
  if (!eu.trocar_senha) {
    const itens = [["#/", "Meus sistemas", (r) => r === "#/"]];
    if (eu.perfil === "admin") {
      itens.push(
        ["#/admin/solucoes", "Soluções", (r) => r.startsWith("#/admin/solucao")],
        ["#/admin/usuarios", "Usuários", (r) => r.startsWith("#/admin/usuarios")]);
    }
    for (const [href, rotulo, ativo] of itens) {
      menu.append(h("a", { href, "aria-current": ativo(rota) ? "page" : null }, rotulo));
    }
  }
  conta.replaceChildren(
    h("span", { class: "conta-nome" }, eu.nome, h("small", {}, eu.perfil === "admin" ? "Administrador" : eu.usuario)),
    eu.trocar_senha ? null : h("a", { href: "#/senha", class: "conta-link" }, "Trocar senha"),
    h("button", {
      type: "button", class: "conta-link",
      onclick: async () => { await api("/api/auth/logout", { method: "POST" }).catch(() => {}); eu = null; location.hash = "#/login"; },
    }, "Sair"));
}

/* ================= login e senha ================= */

function telaLogin() {
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const form = h("form", { class: "login-form" },
    h("div", { class: "campo" }, h("label", { for: "login-usuario" }, "Usuário"),
      h("input", { id: "login-usuario", name: "usuario", type: "text", autocomplete: "username", required: true, autofocus: true })),
    h("div", { class: "campo" }, h("label", { for: "login-senha" }, "Senha"),
      h("input", { id: "login-senha", name: "senha", type: "password", autocomplete: "current-password", required: true })),
    erro,
    h("button", { type: "submit", class: "botao primario largo" }, "Entrar"));
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    erro.hidden = true;
    const botao = form.querySelector("button[type=submit]");
    botao.disabled = true;
    try {
      eu = await api("/api/auth/login", { method: "POST", body: { usuario: form.usuario.value, senha: form.senha.value } });
      if (eu.trocar_senha) location.hash = "#/senha";
      else await seguirDestino();
    } catch (e) {
      erro.textContent = e.message;
      erro.hidden = false;
      form.senha.value = "";
      form.senha.focus();
    } finally {
      botao.disabled = false;
    }
  });
  $conteudo.replaceChildren(
    h("section", { class: "login-pagina" },
      h("div", { class: "login-faixa", "aria-hidden": "true" },
        h("h2", {}, "Os sistemas da LOG20 num só lugar"),
        h("p", {}, "RH, operação e logística com um único acesso.")),
      h("div", { class: "login-lado" },
        h("div", { class: "login-caixa" },
          h("img", { class: "logo-login", src: "/static/marca/logolog20.png", alt: "LOG20 Logística", width: "152", height: "54" }),
          h("h1", {}, "Entrar no portal"),
          h("p", {}, "Use o usuário e a senha que o administrador passou para você."),
          form,
          h("p", { class: "nota" }, "Esqueceu a senha? Peça ao administrador para redefinir.")))));
}

function telaSenha() {
  const obrigatoria = eu.trocar_senha;
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const form = h("form", { class: "painel form-estreito" },
    h("h1", {}, obrigatoria ? "Defina a sua senha" : "Trocar senha"),
    obrigatoria ? h("p", { class: "nota" }, "Você entrou com uma senha temporária. Crie uma senha pessoal para continuar.") : null,
    h("div", { class: "campo" }, h("label", { for: "s-atual" }, obrigatoria ? "Senha temporária" : "Senha atual"),
      h("input", { id: "s-atual", name: "atual", type: "password", autocomplete: "current-password", required: true })),
    h("div", { class: "campo" }, h("label", { for: "s-nova" }, "Nova senha"),
      h("input", { id: "s-nova", name: "nova", type: "password", autocomplete: "new-password", minlength: 8, required: true }),
      h("small", {}, "Pelo menos 8 caracteres.")),
    h("div", { class: "campo" }, h("label", { for: "s-conf" }, "Repita a nova senha"),
      h("input", { id: "s-conf", name: "conf", type: "password", autocomplete: "new-password", required: true })),
    erro,
    h("div", { class: "config-rodape" },
      h("button", { type: "submit", class: "botao primario" }, "Salvar senha"),
      obrigatoria ? null : h("a", { href: "#/", class: "botao" }, "Cancelar")));
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    erro.hidden = true;
    if (form.nova.value !== form.conf.value) {
      erro.textContent = "As senhas não conferem.";
      erro.hidden = false;
      return;
    }
    try {
      await api("/api/auth/senha", { method: "POST", body: { atual: form.atual.value, nova: form.nova.value } });
      eu = await api("/api/auth/eu");
      toast("Senha alterada.");
      await seguirDestino();
    } catch (e) {
      erro.textContent = e.message;
      erro.hidden = false;
    }
  });
  $conteudo.replaceChildren(form);
}

/* ================= meus sistemas ================= */

async function telaMeusSistemas() {
  const dados = await api("/api/meus-sistemas");
  const lista = h("div", { class: "sistemas-lista" }, dados.sistemas.map((s) =>
    h("article", { class: `sistema${s.disponivel ? "" : " fora"}` },
      logoSistema(s),
      h("div", { class: "sistema-info" },
        h("h2", { class: "sistema-nome" }, s.nome,
          s.disponivel ? null : h("span", { class: "pilula", "data-estado": "parado" }, "Fora do ar")),
        h("span", { class: "sistema-area" }, s.categoria),
        h("p", { class: "sistema-resumo" }, s.resumo)),
      h("div", { class: "sistema-acoes" },
        s.disponivel
          ? h("a", { class: "botao acessar", href: s.url, target: "_blank", rel: "noopener" }, "Acessar")
          : h("button", { class: "botao", type: "button", disabled: true }, "Indisponível")))));
  $conteudo.replaceChildren(
    h("section", { class: "intro" },
      h("h1", {}, `Olá, ${eu.nome.split(" ")[0]}`),
      h("p", {}, eu.perfil === "admin"
        ? "Como administrador, você vê todos os sistemas. Para instalar, iniciar ou configurar, use Soluções."
        : "Estes são os sistemas liberados para você. Cada um abre em uma nova aba.")),
    dados.sistemas.length ? lista : h("p", { class: "vazio" }, "Nenhum sistema foi liberado para você ainda. Peça acesso ao administrador."));
}

/* ================= admin: soluções ================= */

function botoesAcao(sol, aoAgir) {
  const caixa = h("div", { class: "sistema-acoes" });
  if (sol.status.estado === "ocupado") {
    caixa.append(h("button", { class: "botao", type: "button", disabled: true }, `${sol.status.detalhe}…`));
    return caixa;
  }
  for (const [acao, rotulo, estilo] of ACOES_POR_ESTADO[sol.status.estado] || []) {
    const cls = `botao ${estilo || ""}`.trim();
    if (acao === "abrir") {
      caixa.append(h("a", { class: cls, href: sol.url, target: "_blank", rel: "noopener" }, rotulo));
      continue;
    }
    caixa.append(h("button", {
      class: cls, type: "button",
      onclick: async (ev) => {
        ev.target.disabled = true;
        try {
          await api(`/api/solucoes/${sol.id}/acoes/${acao}`, { method: "POST" });
          emAndamento = true;
          aoAgir?.();
        } catch (e) { toast(e.message); }
        atualizar().then(agendar);
      },
    }, rotulo));
  }
  return caixa;
}

async function indicadorDocker() {
  try {
    const d = await api("/api/docker");
    return h("div", { class: `docker ${d.ok ? "ok" : "falha"}`, title: d.erro || "" },
      d.ok ? `Docker ${d.versao}` : "Docker não está rodando no servidor");
  } catch { return h("div", { class: "docker falha" }, "Sem resposta"); }
}

async function telaSolucoes() {
  $conteudo.replaceChildren(
    h("section", { class: "intro intro-linha" },
      h("div", {}, h("h1", {}, "Soluções"), h("p", {}, "Instale, inicie e acompanhe cada solução publicada no portal.")),
      h("div", { class: "intro-lado" },
        h("div", { id: "docker-slot" }),
        h("button", {
          type: "button", class: "botao pequeno",
          title: "Lê de novo a pasta solucoes/ para incluir soluções novas ou alteradas",
          onclick: async (ev) => {
            ev.target.disabled = true;
            try {
              const r = await api("/api/catalogo/recarregar", { method: "POST" });
              await atualizarSolucoes();
              const problemas = r.erros_catalogo.length + (r.proxy?.erro ? 1 : 0);
              toast(problemas
                ? `Catálogo recarregado com ${problemas} problema(s); veja o aviso no topo.`
                : `Catálogo recarregado: ${r.total} solução(ões).${r.proxy?.aplicado ? " Acesso pelo Caddy atualizado." : ""}`);
            } catch (e) { toast(e.message); }
            ev.target.disabled = false;
          },
        }, "Recarregar catálogo"))),
    h("div", { id: "erros-catalogo" }),
    h("div", { class: "sistemas-lista", id: "grade-solucoes" }));
  await atualizarSolucoes();
}

async function atualizarSolucoes() {
  const [dados, docker] = await Promise.all([api("/api/solucoes"), indicadorDocker()]);
  const grade = document.getElementById("grade-solucoes");
  if (!grade) return;
  emAndamento = dados.solucoes.some((s) => EM_ANDAMENTO.has(s.status.estado));
  document.getElementById("docker-slot").replaceChildren(docker);
  const erros = document.getElementById("erros-catalogo");
  erros.replaceChildren();
  if (dados.proxy?.erro) {
    erros.append(h("div", { class: "aviso" }, `Acesso às soluções (Caddy): ${dados.proxy.mensagem}`));
  }
  if (dados.erros_catalogo.length) {
    erros.append(h("div", { class: "aviso" }, "Alguns manifestos não puderam ser lidos:",
      h("ul", {}, dados.erros_catalogo.map((m) => h("li", {}, m)))));
  }
  if (!dados.solucoes.length) {
    grade.replaceChildren(h("p", { class: "vazio" }, "Nenhuma solução cadastrada. Crie um arquivo solucoes/<id>/solucao.toml."));
    return;
  }
  grade.replaceChildren(...dados.solucoes.map((sol) => {
    const acoes = botoesAcao(sol);
    acoes.append(h("a", { class: "botao", href: `#/admin/solucao/${sol.id}` }, "Gerenciar"));
    return h("article", { class: "sistema" },
      logoSistema(sol),
      h("div", { class: "sistema-info" },
        h("h2", { class: "sistema-nome" }, h("a", { href: `#/admin/solucao/${sol.id}` }, sol.nome), pilula(sol.status)),
        h("span", { class: "sistema-area" }, sol.categoria),
        h("p", { class: "sistema-resumo" }, sol.resumo)),
      acoes);
  }));
}

let abaLogs = "operacao";
let ultimosLogs = { operacao: null, aplicacao: "" };

async function telaSolucao(id) {
  const sol = await api(`/api/solucoes/${id}`);
  document.title = `${sol.nome} · Portal de Soluções LOG20`;

  const ficha = h("dl", { class: "ficha" });
  const repo = sol.codigo ? `${sol.codigo.repositorio.replace(/^https:\/\//, "").replace(/\.git$/, "")} (${sol.codigo.ref})` : "";
  const vi = sol.versao_instalada;
  const instalada = vi ? `${vi.commit}: ${vi.resumo}${vi.data ? ` (${quando(Date.parse(vi.data) / 1000)})` : ""}` : "";
  for (const [k, v] of [["Versão", sol.versao], ["Responsável", sol.responsavel], ["Endereço", sol.url],
                        ["Código", repo], ["Instalado", instalada], ["Imagem", sol.imagem]]) {
    if (v) ficha.append(h("dt", {}, k), h("dd", {}, v));
  }

  const demo = sol.acessos_demo.length
    ? h("div", { class: "demo" },
        h("h3", {}, "Acessos de demonstração"),
        h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "Perfil"), h("th", {}, "Usuário"), h("th", {}, "Senha"))),
          h("tbody", {}, sol.acessos_demo.map((a) => h("tr", {}, h("td", {}, a.perfil), h("td", {}, h("code", {}, a.usuario)), h("td", {}, h("code", {}, a.senha)))))),
        h("p", { class: "nota" }, "Login próprio da solução. Válidos no primeiro acesso; ela pede para trocar a senha."))
    : null;

  const formConfig = h("form", { class: "config" });
  montarFormulario(formConfig, sol);

  const formEndereco = h("form", { class: "config" },
    h("div", { class: "campo" },
      h("label", { for: "url-publica" }, "Endereço público"),
      h("input", { id: "url-publica", name: "url_publica", type: "text", value: sol.url_publica, placeholder: sol.url_padrao, autocomplete: "off" }),
      h("small", {}, `Em branco usa ${sol.url_padrao}. Com domínio próprio, informe por exemplo https://rh.suaempresa.com.br.`)),
    h("div", { class: "config-rodape" }, h("button", { type: "submit", class: "botao" }, "Salvar endereço")));
  formEndereco.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      const r = await api(`/api/solucoes/${sol.id}/endereco`, { method: "PUT", body: { url_publica: formEndereco.url_publica.value } });
      toast(r.aviso);
    } catch (e) { toast(e.message); }
  });

  const abas = h("div", { class: "abas", role: "tablist" },
    [["operacao", "Operação"], ["aplicacao", "Aplicação"]].map(([aba, rotulo]) =>
      h("button", { type: "button", role: "tab", "data-aba": aba, onclick: () => { abaLogs = aba; pintarLogs(true); } }, rotulo)));

  $conteudo.replaceChildren(
    h("a", { class: "voltar", href: "#/admin/solucoes" }, "← Todas as soluções"),
    h("section", { class: "cabecalho" },
      logoSistema(sol, true),
      h("div", { class: "cabecalho-texto" },
        h("h1", {}, sol.nome),
        h("span", { class: "sistema-area" }, sol.categoria),
        h("p", { class: "sistema-resumo" }, sol.resumo)),
      h("div", { class: "estado", id: "estado" })),
    h("div", { class: "acoes", id: "acoes" }),
    h("div", { class: "colunas" },
      h("section", { class: "painel" }, h("h2", {}, "Sobre"), h("p", { class: "descricao" }, sol.descricao), ficha, demo),
      h("div", { class: "pilha" },
        h("section", { class: "painel" }, h("h2", {}, "Configurações"), formConfig),
        h("section", { class: "painel" }, h("h2", {}, "Acesso"), formEndereco))),
    h("section", { class: "painel logs" },
      h("div", { class: "logs-topo" }, h("h2", {}, "Logs"), abas),
      h("pre", { class: "saida", tabindex: "0" }, "Carregando…")));

  ultimosLogs = { operacao: null, aplicacao: "" };
  abaLogs = sol.status.estado === "rodando" ? "aplicacao" : "operacao";
  pintarStatusSolucao(sol);
  await atualizarLogs(id);
}

function montarFormulario(form, sol) {
  form.replaceChildren();
  if (!sol.configuracoes.length) {
    form.append(h("p", { class: "nota" }, "Esta solução não tem configurações."));
    return;
  }
  for (const c of sol.configuracoes) {
    const idCampo = `cfg-${c.nome}`;
    const ajuda = (c.ajuda || (c.tipo === "segredo" && c.definido))
      ? h("small", {}, c.ajuda ? `${c.ajuda} ` : "",
          c.tipo === "segredo" && c.definido
            ? h("button", {
                type: "button", class: "link-limpar",
                onclick: async () => {
                  await api(`/api/solucoes/${sol.id}/configuracoes/${c.nome}`, { method: "DELETE" });
                  montarFormulario(form, await api(`/api/solucoes/${sol.id}`));
                  toast("Valor removido. Reinicie a solução para aplicar.");
                },
              }, "Remover valor salvo")
            : null)
      : null;
    if (c.tipo === "booleano") {
      form.append(h("div", { class: "campo campo-check" },
        h("input", { id: idCampo, name: c.nome, type: "checkbox", checked: c.valor === "true" }),
        h("label", { for: idCampo }, c.rotulo), ajuda));
    } else {
      form.append(h("div", { class: "campo" },
        h("label", { for: idCampo }, c.rotulo),
        h("input", {
          id: idCampo, name: c.nome, autocomplete: "off",
          type: c.tipo === "segredo" ? "password" : "text",
          value: c.tipo === "segredo" ? "" : (c.valor ?? ""),
          placeholder: c.tipo === "segredo" ? (c.definido ? "•••••••• (definida — deixe em branco para manter)" : "Não definida") : null,
        }), ajuda));
    }
  }
  form.append(h("div", { class: "config-rodape" },
    h("button", { type: "submit", class: "botao primario" }, "Salvar"),
    h("span", { class: "config-msg" }, "Mudanças valem no próximo início.")));
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const valores = {};
    for (const c of sol.configuracoes) {
      const input = form.elements[c.nome];
      valores[c.nome] = c.tipo === "booleano" ? input.checked : input.value;
    }
    try {
      const r = await api(`/api/solucoes/${sol.id}/configuracoes`, { method: "PUT", body: { valores } });
      montarFormulario(form, await api(`/api/solucoes/${sol.id}`));
      toast(r.aviso);
    } catch (e) { toast(e.message); }
  };
}

function pintarStatusSolucao(sol) {
  emAndamento = EM_ANDAMENTO.has(sol.status.estado);
  const estado = document.getElementById("estado");
  if (!estado) return;
  const detalhe = sol.status.detalhe === ROTULOS[sol.status.estado] ? "" : sol.status.detalhe;
  estado.replaceChildren(pilula(sol.status), h("small", {}, detalhe));
  document.getElementById("acoes").replaceChildren(botoesAcao(sol, () => { abaLogs = "operacao"; }));
}

async function atualizarLogs(id) {
  try {
    const l = await api(`/api/solucoes/${id}/logs`);
    ultimosLogs = { operacao: l.operacao, aplicacao: l.aplicacao };
    pintarLogs(false);
  } catch { /* mantém o último conteúdo */ }
}

function pintarLogs(forcarFim) {
  const pre = $conteudo.querySelector(".saida");
  if (!pre) return;
  for (const b of $conteudo.querySelectorAll(".abas button")) {
    b.setAttribute("aria-selected", String(b.dataset.aba === abaLogs));
  }
  const op = ultimosLogs.operacao;
  const texto = abaLogs === "operacao"
    ? (op ? op.log || `${op.acao}: ${op.estado}` : "Nenhuma operação desde que o portal foi iniciado.")
    : (ultimosLogs.aplicacao || "O container ainda não gerou logs.");
  const noFim = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  if (pre.textContent !== texto) {
    pre.textContent = texto;
    if (noFim || forcarFim) pre.scrollTop = pre.scrollHeight;
  } else if (forcarFim) {
    pre.scrollTop = pre.scrollHeight;
  }
}

/* ================= admin: usuários ================= */

async function telaUsuarios() {
  const dados = await api("/api/usuarios");
  const nomes = Object.fromEntries(dados.solucoes.map((s) => [s.id, s.nome]));
  const area = h("div", { id: "form-usuario" });

  const abrirForm = (u) => {
    area.replaceChildren(formUsuario(u, dados.solucoes, area));
    area.querySelector("input:not([disabled])")?.focus();
  };

  const linhas = dados.usuarios.map((u) => h("tr", { class: u.ativo ? "" : "inativo" },
    h("td", {}, h("strong", {}, u.nome), h("div", { class: "sub" }, u.usuario)),
    h("td", {}, u.perfil === "admin" ? "Administrador" : "Usuário"),
    h("td", {}, u.perfil === "admin" ? h("span", { class: "sub" }, "Todos")
      : (u.solucoes.length ? u.solucoes.map((s) => nomes[s] || s).join(", ") : h("span", { class: "sub" }, "Nenhum"))),
    h("td", {}, quando(u.ultimo_login)),
    h("td", {}, h("span", { class: "pilula", "data-estado": u.ativo ? (u.trocar_senha ? "iniciando" : "rodando") : "parado" },
      u.ativo ? (u.trocar_senha ? "Senha temporária" : "Ativo") : "Desativado")),
    h("td", { class: "td-acoes" },
      h("button", { type: "button", class: "botao pequeno", onclick: () => abrirForm(u) }, "Editar"),
      u.id === eu.id ? null : h("button", {
        type: "button", class: "botao pequeno",
        onclick: async () => {
          try {
            const r = await api(`/api/usuarios/${u.id}/redefinir-senha`, { method: "POST" });
            area.replaceChildren(painelSenha(`Nova senha temporária de ${u.nome}`, r.senha_temporaria));
            area.scrollIntoView({ behavior: "smooth", block: "nearest" });
            await recarregarTabela();
          } catch (e) { toast(e.message); }
        },
      }, "Redefinir senha"))));

  const recarregarTabela = async () => {
    const mantido = area.firstChild;
    await telaUsuarios();
    if (mantido) document.getElementById("form-usuario").replaceChildren(mantido);
  };
  area._recarregar = recarregarTabela;

  $conteudo.replaceChildren(
    h("section", { class: "intro intro-linha" },
      h("div", {}, h("h1", {}, "Usuários"), h("p", {}, "Quem pode entrar no portal e quais sistemas cada pessoa vê.")),
      h("button", { type: "button", class: "botao primario", onclick: () => abrirForm(null) }, "Novo usuário")),
    area,
    h("div", { class: "painel tabela-rolagem" },
      h("table", { class: "tabela-usuarios" },
        h("thead", {}, h("tr", {}, ["Nome", "Perfil", "Sistemas", "Último acesso", "Situação", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, linhas))));
}

function formUsuario(u, solucoes, area) {
  const novo = !u;
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const marcadas = new Set(u?.solucoes || []);
  const listaSistemas = h("fieldset", { class: "sistemas" },
    h("legend", {}, "Sistemas liberados"),
    solucoes.length
      ? solucoes.map((s) => h("label", { class: "campo-check" },
          h("input", { type: "checkbox", name: "solucoes", value: s.id, checked: marcadas.has(s.id) }), s.nome))
      : h("p", { class: "nota" }, "Nenhuma solução cadastrada."),
    h("small", { class: "nota-admin", hidden: true }, "Administradores acessam todos os sistemas."));

  const form = h("form", { class: "painel form-usuario" },
    h("h2", {}, novo ? "Novo usuário" : `Editar ${u.nome}`),
    h("div", { class: "form-grade" },
      h("div", { class: "campo" }, h("label", { for: "u-usuario" }, "Usuário (login)"),
        h("input", { id: "u-usuario", name: "usuario", type: "text", value: u?.usuario || "", required: true, disabled: !novo, autocomplete: "off" }),
        novo ? h("small", {}, "Ex.: maria.silva ou a matrícula.") : null),
      h("div", { class: "campo" }, h("label", { for: "u-nome" }, "Nome"),
        h("input", { id: "u-nome", name: "nome", type: "text", value: u?.nome || "", required: true, autocomplete: "off" })),
      h("div", { class: "campo" }, h("label", { for: "u-perfil" }, "Perfil"),
        h("select", { id: "u-perfil", name: "perfil" },
          h("option", { value: "usuario", selected: (u?.perfil || "usuario") === "usuario" }, "Usuário"),
          h("option", { value: "admin", selected: u?.perfil === "admin" }, "Administrador"))),
      novo ? null : h("div", { class: "campo campo-check alinhado" },
        h("input", { id: "u-ativo", name: "ativo", type: "checkbox", checked: u.ativo }),
        h("label", { for: "u-ativo" }, "Pode entrar no portal"))),
    listaSistemas,
    erro,
    h("div", { class: "config-rodape" },
      h("button", { type: "submit", class: "botao primario" }, novo ? "Criar usuário" : "Salvar"),
      h("button", { type: "button", class: "botao", onclick: () => area.replaceChildren() }, "Cancelar")));

  const sincronizarPerfil = () => {
    const admin = form.perfil.value === "admin";
    for (const c of listaSistemas.querySelectorAll("input")) c.disabled = admin;
    listaSistemas.querySelector(".nota-admin").hidden = !admin;
  };
  form.perfil.addEventListener("change", sincronizarPerfil);
  sincronizarPerfil();

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    erro.hidden = true;
    const solucoesSel = [...listaSistemas.querySelectorAll("input:checked")].map((c) => c.value);
    try {
      if (novo) {
        const r = await api("/api/usuarios", {
          method: "POST",
          body: { usuario: form.usuario.value, nome: form.nome.value, perfil: form.perfil.value, solucoes: solucoesSel },
        });
        area.replaceChildren(painelSenha(`Usuário ${r.usuario.usuario} criado. Senha temporária:`, r.senha_temporaria));
      } else {
        await api(`/api/usuarios/${u.id}`, {
          method: "PATCH",
          body: { nome: form.nome.value, perfil: form.perfil.value, ativo: form.ativo.checked, solucoes: solucoesSel },
        });
        area.replaceChildren();
        toast("Usuário atualizado.");
        if (u.id === eu.id) eu = await api("/api/auth/eu");
      }
      await area._recarregar();
    } catch (e) {
      erro.textContent = e.message;
      erro.hidden = false;
    }
  });
  return form;
}

/* ================= navegação ================= */

async function atualizar() {
  try {
    if (rota.startsWith("#/admin/solucao/")) {
      const id = rota.split("/")[3];
      pintarStatusSolucao(await api(`/api/solucoes/${id}`));
      await atualizarLogs(id);
    } else if (rota === "#/admin/solucoes") {
      await atualizarSolucoes();
    } else if (rota === "#/") {
      await telaMeusSistemas();
    }
  } catch { /* tenta de novo no próximo ciclo */ }
}

/** Depois do login, volta ao sistema que a pessoa tentou abrir, se ele for um dos liberados para ela. */
async function seguirDestino() {
  const destino = voltarPara;
  voltarPara = "";
  if (destino) {
    try {
      const alvo = new URL(destino);
      const { sistemas } = await api("/api/meus-sistemas");
      const sistema = sistemas.find((s) => {
        try { return new URL(s.url, location.href).origin === alvo.origin; } catch { return false; }
      });
      if (sistema && /^https?:$/.test(alvo.protocol)) {
        if (sistema.disponivel) {
          location.href = alvo.href;
          return;
        }
        // Voltar para um sistema parado so levaria a pagina de erro do proxy.
        setTimeout(() => toast(eu?.perfil === "admin"
          ? `${sistema.nome} está fora do ar. Inicie-o em Soluções.`
          : `${sistema.nome} está fora do ar no momento. Tente de novo mais tarde.`), 300);
      }
    } catch { /* destino inválido: segue para o início */ }
  }
  if (location.hash === "#/" || location.hash === "") navegar();
  else location.hash = "#/";
}

function agendar() {
  clearTimeout(timer);
  if (!ritmo) return;
  timer = setTimeout(async () => {
    if (!document.hidden) await atualizar();
    agendar();
  }, emAndamento ? ritmo[0] : ritmo[1]);
}

// Aba em segundo plano não consulta o servidor; ao voltar, atualiza na hora.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && ritmo) atualizar().then(agendar);
});

async function navegar() {
  clearTimeout(timer);
  ritmo = null;
  const [caminho, consulta] = (location.hash || "#/").split("?");
  rota = caminho;
  const voltar = new URLSearchParams(consulta || "").get("voltar");
  if (voltar) voltarPara = voltar;
  document.title = "Portal de Soluções LOG20";

  if (!eu) {
    // Também na tela de login: quem já tem sessão segue direto.
    try { eu = await api("/api/auth/eu"); } catch { eu = null; }
    if (!eu && rota !== "#/login") { location.hash = "#/login"; return; }
  }
  if (eu && rota === "#/login") {
    if (eu.trocar_senha) location.hash = "#/senha";
    else await seguirDestino();
    return;
  }
  if (eu?.trocar_senha && rota !== "#/senha") { location.hash = "#/senha"; return; }
  if (rota.startsWith("#/admin") && eu?.perfil !== "admin") { location.hash = "#/"; return; }

  montarTopo();
  $conteudo.classList.toggle("conteudo-cheio", rota === "#/login");
  let novoRitmo = null;
  try {
    if (rota === "#/login") telaLogin();
    else if (rota === "#/senha") telaSenha();
    else if (rota === "#/admin/solucoes") { await telaSolucoes(); novoRitmo = [3000, 30000]; }
    else if (rota.startsWith("#/admin/solucao/")) { await telaSolucao(rota.split("/")[3]); novoRitmo = [2500, 20000]; }
    else if (rota === "#/admin/usuarios") await telaUsuarios();
    else { rota = "#/"; await telaMeusSistemas(); novoRitmo = [60000, 60000]; }
  } catch (e) {
    if (e.status === 401 || location.hash !== rota) return;
    $conteudo.replaceChildren(h("p", { class: "aviso" }, e.message), h("a", { class: "voltar", href: "#/" }, "← Voltar ao início"));
  }
  ritmo = novoRitmo;
  agendar();
}

window.addEventListener("hashchange", navegar);
navegar();
