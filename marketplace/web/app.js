"use strict";

/* =========================================================================
   Portal de Soluções LOG20 — interface (sem frameworks)
   Rotas: #/  #/login  #/senha  #/admin/solucoes  #/admin/solucao/<id>
          #/admin/usuarios  #/admin/acessos
   ========================================================================= */

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

const USUARIO_RE = /^[a-zA-Z0-9._@-]{3,60}$/;
const PERFIL_TEXTO = { "": "usuario", usuario: "usuario", "usuário": "usuario", admin: "admin", administrador: "admin", administradora: "admin" };

const $conteudo = document.getElementById("conteudo");
let eu = null;
let timer = null;
let ritmo = null;          // [ms com algo em andamento, ms parado]
let emAndamento = false;
let voltarPara = "";       // sistema que a pessoa tentou abrir antes do login (#/login?voltar=...)
const EM_ANDAMENTO = new Set(["ocupado", "iniciando"]);
let rota = "";

/* ================= utilidades ================= */

// replaceChildren ignorando null/false, para montar telas com partes opcionais (cond ? el : null).
const _replaceChildren = Element.prototype.replaceChildren;
Element.prototype.replaceChildren = function (...filhos) {
  return _replaceChildren.apply(this, filhos.flat(Infinity).filter((f) => f !== null && f !== undefined && f !== false));
};

/** h("a", {href: "#", class: "x", onclick: fn}, "texto", outroEl) */
function h(tag, attrs = {}, ...filhos) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const f of filhos.flat(Infinity)) {
    if (f === null || f === undefined || f === false) continue;
    el.append(f instanceof Node ? f : document.createTextNode(String(f)));
  }
  return el;
}

class ErroApi extends Error {
  constructor(msg, status, dados) { super(msg); this.status = status; this.dados = dados; }
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
    throw new ErroApi(msg, resp.status, dados);
  }
  return dados;
}

function toast(msg, tipo = "") {
  document.querySelector(".toast")?.remove();
  const el = h("div", { class: `toast ${tipo}`.trim(), role: "status" }, msg);
  document.body.append(el);
  setTimeout(() => el.remove(), 5000);
}

function pilula(estado, texto) {
  return h("span", { class: "pilula", "data-estado": estado }, texto || ROTULOS[estado] || estado);
}

function plural(n, um, varios) { return `${n} ${n === 1 ? um : varios}`; }

function iniciais(nome) {
  const partes = String(nome || "?").replace(/\(.*?\)/g, "").trim().split(/\s+/).filter((p) => p.length > 1);
  if (!partes.length) return "?";
  return (partes.length > 1 ? partes[0][0] + partes[partes.length - 1][0] : partes[0].slice(0, 2)).toUpperCase();
}

function avatar(u, extra = "") {
  return h("span", { class: `avatar ${extra}`.trim(), "aria-hidden": "true" }, iniciais(u.nome));
}

/** Logo do sistema ou, sem logo, um monograma nas cores da marca. */
function logoSistema(s, tamanho = "") {
  const cls = `logo-sistema ${tamanho}`.trim();
  if (s.logo) return h("span", { class: cls }, h("img", { src: s.logo, alt: "", loading: "lazy" }));
  return h("span", { class: `${cls} monograma`, "aria-hidden": "true" }, iniciais(s.nome));
}

function quando(ts, curto = false) {
  if (!ts) return "Nunca";
  const d = new Date(ts * 1000);
  const hoje = new Date();
  const hora = d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === hoje.toDateString()) return `Hoje, ${hora}`;
  const ontem = new Date(hoje); ontem.setDate(hoje.getDate() - 1);
  if (d.toDateString() === ontem.toDateString()) return `Ontem, ${hora}`;
  return curto ? d.toLocaleDateString("pt-BR") : `${d.toLocaleDateString("pt-BR")}, ${hora}`;
}

function normalizar(txt) {
  return String(txt || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

async function copiar(texto, botao) {
  try {
    await navigator.clipboard.writeText(texto);
    if (botao) { const antes = botao.textContent; botao.textContent = "Copiado"; setTimeout(() => { botao.textContent = antes; }, 1800); }
  } catch { toast("Não foi possível copiar. Selecione o texto e copie manualmente."); }
}

/** Baixa um CSV que o Excel em português abre direto (separador ";" e BOM). */
function baixarCsv(nome, linhas) {
  const esc = (v) => { const s = String(v ?? ""); return /[;"\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const texto = "﻿" + linhas.map((l) => l.map(esc).join(";")).join("\r\n") + "\r\n";
  const url = URL.createObjectURL(new Blob([texto], { type: "text/csv;charset=utf-8" }));
  const a = h("a", { href: url, download: nome, hidden: true });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
}

function hojeIso() { return new Date().toISOString().slice(0, 10); }

function painelSenha(titulo, senha, extra) {
  return h("div", { class: "senha-temp", role: "alert" },
    h("strong", {}, titulo),
    h("div", { class: "senha-linha" },
      h("code", {}, senha),
      h("button", { type: "button", class: "botao pequeno", onclick: (ev) => copiar(senha, ev.currentTarget) }, "Copiar")),
    h("small", {}, extra || "Anote agora: ela não será mostrada de novo. No primeiro acesso a pessoa cria uma senha própria."));
}

/* ----- diálogos (elemento <dialog> nativo) ----- */

function dialogo({ titulo, descricao, corpo, rodape, largo = false, aoFechar }) {
  const dlg = h("dialog", { class: `dialogo${largo ? " largo" : ""}`, "aria-labelledby": "dlg-titulo" },
    h("div", { class: "dialogo-topo" },
      h("div", {},
        h("h2", { id: "dlg-titulo" }, titulo),
        descricao ? h("p", {}, descricao) : null),
      h("button", { type: "button", class: "fechar", "aria-label": "Fechar", onclick: () => dlg.close() }, "×")),
    h("div", { class: "dialogo-corpo" }, corpo),
    rodape ? h("div", { class: "dialogo-rodape" }, rodape) : null);
  dlg.addEventListener("close", () => { dlg.remove(); aoFechar?.(); });
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

function confirmar({ titulo, texto, botao, perigo = false }) {
  return new Promise((resolve) => {
    let ok = false;
    const dlg = dialogo({
      titulo,
      corpo: h("p", { class: "dialogo-texto" }, texto),
      rodape: [
        h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"),
        h("button", { type: "button", class: `botao ${perigo ? "perigo-cheio" : "primario"}`, onclick: () => { ok = true; dlg.close(); } }, botao),
      ],
      aoFechar: () => resolve(ok),
    });
  });
}

/** Lista de sistemas com caixas de marcar; devolve {el, marcados()}. */
function escolhaSistemas(solucoes, marcados = new Set(), nome = "solucoes") {
  const el = h("div", { class: "escolha-sistemas" },
    solucoes.length
      ? solucoes.map((s) => h("label", { class: "escolha" },
          h("input", { type: "checkbox", name: nome, value: s.id, checked: marcados.has(s.id) }),
          logoSistema(s, "mini"),
          h("span", {}, s.nome)))
      : h("p", { class: "nota" }, "Nenhuma solução cadastrada ainda."));
  return { el, marcados: () => [...el.querySelectorAll("input:checked")].map((c) => c.value) };
}

/* ================= cabeçalho ================= */

function montarTopo() {
  const topo = document.getElementById("topo");
  const menu = document.getElementById("menu");
  const conta = document.getElementById("conta");
  if (!eu || rota === "#/login") { topo.hidden = true; return; }
  topo.hidden = false;
  menu.replaceChildren();
  if (!eu.trocar_senha) {
    const item = (href, rotulo, ativo) => h("a", { href, "aria-current": ativo ? "page" : null }, rotulo);
    menu.append(item("#/", "Meus sistemas", rota === "#/"));
    if (eu.perfil === "admin") {
      menu.append(h("span", { class: "menu-divisa", "aria-hidden": "true" }),
        item("#/admin/solucoes", "Soluções", rota.startsWith("#/admin/solucao")),
        item("#/admin/usuarios", "Usuários", rota === "#/admin/usuarios"),
        item("#/admin/acessos", "Acessos", rota === "#/admin/acessos"));
    }
  }
  const sair = async () => { await api("/api/auth/logout", { method: "POST" }).catch(() => {}); eu = null; location.hash = "#/login"; };
  const detalhes = h("details", { class: "conta-menu" },
    h("summary", { "aria-label": "Sua conta" },
      avatar(eu),
      h("span", { class: "conta-nome" }, eu.nome, h("small", {}, eu.perfil === "admin" ? "Administrador" : eu.usuario))),
    h("div", { class: "conta-lista" },
      h("div", { class: "conta-cabeca" }, h("strong", {}, eu.nome), h("small", {}, eu.usuario)),
      eu.trocar_senha ? null : h("a", { href: "#/senha" }, "Trocar senha"),
      h("button", { type: "button", onclick: sair }, "Sair")));
  // Fecha o menu ao clicar fora ou escolher uma opção.
  detalhes.addEventListener("click", (ev) => { if (ev.target.closest(".conta-lista a, .conta-lista button")) detalhes.open = false; });
  conta.replaceChildren(detalhes);
}

document.addEventListener("click", (ev) => {
  const aberto = document.querySelector(".conta-menu[open]");
  if (aberto && !aberto.contains(ev.target)) aberto.open = false;
});

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
    h("div", { class: "linha-botoes" },
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

let buscaSistemas = "";

async function telaMeusSistemas() {
  const dados = await api("/api/meus-sistemas");
  const primeiraVez = !document.getElementById("grade-meus");
  if (primeiraVez) {
    const busca = dados.sistemas.length > 6
      ? h("input", {
          type: "search", class: "busca", placeholder: "Procurar sistema", "aria-label": "Procurar sistema", value: buscaSistemas,
          oninput: (ev) => { buscaSistemas = ev.target.value; pintarMeusSistemas(dados.sistemas); },
        })
      : null;
    $conteudo.replaceChildren(
      h("section", { class: "intro intro-linha" },
        h("div", {},
          h("h1", {}, `Olá, ${eu.nome.split(" ")[0]}`),
          h("p", {}, eu.perfil === "admin"
            ? "Como administrador, você vê todos os sistemas. Para instalar ou configurar, use Soluções."
            : "Estes são os sistemas liberados para você. Cada um abre em uma nova aba.")),
        busca),
      h("div", { id: "grade-meus" }));
  }
  pintarMeusSistemas(dados.sistemas);
}

function pintarMeusSistemas(sistemas) {
  const alvo = document.getElementById("grade-meus");
  if (!alvo) return;
  if (!sistemas.length) {
    alvo.replaceChildren(h("div", { class: "vazio" },
      h("strong", {}, "Nenhum sistema liberado ainda"),
      h("p", {}, "Peça ao administrador do portal para liberar os sistemas que você usa.")));
    return;
  }
  const termo = normalizar(buscaSistemas);
  const visiveis = sistemas.filter((s) => !termo || normalizar(`${s.nome} ${s.categoria} ${s.resumo}`).includes(termo));
  const grupos = new Map();
  for (const s of visiveis) {
    if (!grupos.has(s.categoria)) grupos.set(s.categoria, []);
    grupos.get(s.categoria).push(s);
  }
  const mostrarGrupos = grupos.size > 1;
  alvo.replaceChildren(
    ...[...grupos].map(([categoria, lista]) => h("section", { class: "grupo" },
      mostrarGrupos ? h("h2", { class: "grupo-titulo" }, categoria, h("span", {}, lista.length)) : null,
      h("div", { class: "tiles" }, lista.map(tileSistema)))),
    visiveis.length ? null : h("p", { class: "nota centro" }, "Nenhum sistema encontrado com esse nome."));
}

function tileSistema(s) {
  const corpo = [
    h("div", { class: "tile-topo" },
      logoSistema(s, "medio"),
      s.disponivel ? null : pilula("parado", "Fora do ar")),
    h("h3", { class: "tile-nome" }, s.nome),
    h("p", { class: "tile-resumo" }, s.resumo || s.categoria),
    h("span", { class: "tile-acao" }, s.disponivel ? "Abrir em nova aba" : "Indisponível no momento"),
  ];
  return s.disponivel
    ? h("a", { class: "tile", href: s.url, target: "_blank", rel: "noopener" }, corpo)
    : h("div", { class: "tile fora", "aria-disabled": "true" }, corpo);
}

/* ================= admin: soluções ================= */

let filtroSolucoes = "todas";

function botoesAcao(sol, aoAgir, soPrincipal = false) {
  const caixa = h("div", { class: "acoes-solucao" });
  if (sol.status.estado === "ocupado") {
    caixa.append(h("button", { class: "botao andamento", type: "button", disabled: true, title: sol.status.detalhe },
      h("span", { class: "giro", "aria-hidden": "true" }), h("span", {}, "Aguarde…")));
    return caixa;
  }
  const acoes = ACOES_POR_ESTADO[sol.status.estado] || [];
  for (const [acao, rotulo, estilo] of soPrincipal ? acoes.slice(0, 1) : acoes) {
    const cls = `botao ${estilo || ""}`.trim();
    if (acao === "abrir") {
      caixa.append(h("a", { class: cls, href: sol.url, target: "_blank", rel: "noopener" }, rotulo));
      continue;
    }
    caixa.append(h("button", {
      class: cls, type: "button",
      onclick: async (ev) => {
        if (acao === "remover" && !(await confirmar({
          titulo: "Remover o container?",
          texto: `O container de ${sol.nome} será apagado. A imagem e os dados (volumes) continuam; dá para iniciar de novo depois.`,
          botao: "Remover container", perigo: true,
        }))) return;
        ev.target.disabled = true;
        try {
          await api(`/api/solucoes/${sol.id}/acoes/${acao}`, { method: "POST" });
          emAndamento = true;
          aoAgir?.();
        } catch (e) { toast(e.message, "erro"); }
        atualizar().then(agendar);
      },
    }, rotulo));
  }
  return caixa;
}

async function indicadorDocker() {
  try {
    const d = await api("/api/docker");
    return h("span", { class: `docker ${d.ok ? "ok" : "falha"}`, title: d.erro || "" },
      d.ok ? `Docker ${d.versao}` : "Docker não está rodando no servidor");
  } catch { return h("span", { class: "docker falha" }, "Sem resposta do servidor"); }
}

async function telaSolucoes() {
  $conteudo.replaceChildren(
    h("section", { class: "intro intro-linha" },
      h("div", {}, h("h1", {}, "Soluções"), h("p", {}, "Instale, inicie e acompanhe cada sistema publicado no portal.")),
      h("div", { class: "intro-lado" },
        h("span", { id: "docker-slot" }),
        h("button", {
          type: "button", class: "botao",
          title: "Lê de novo a pasta solucoes/ para incluir soluções novas ou alteradas",
          onclick: async (ev) => {
            ev.currentTarget.disabled = true;
            const botao = ev.currentTarget;
            try {
              const r = await api("/api/catalogo/recarregar", { method: "POST" });
              await atualizarSolucoes();
              const problemas = r.erros_catalogo.length + (r.proxy?.erro ? 1 : 0);
              toast(problemas
                ? `Catálogo recarregado com ${plural(problemas, "problema", "problemas")}. Veja o aviso acima da lista.`
                : `Catálogo recarregado: ${plural(r.total, "solução", "soluções")}.`);
            } catch (e) { toast(e.message, "erro"); }
            botao.disabled = false;
          },
        }, "Recarregar catálogo"))),
    h("div", { id: "erros-catalogo" }),
    h("div", { class: "filtros", id: "filtros-solucoes", role: "group", "aria-label": "Filtrar por situação" }),
    h("div", { class: "lista-solucoes", id: "grade-solucoes" }));
  await atualizarSolucoes();
}

const GRUPOS_ESTADO = {
  todas: { rotulo: "Todas", inclui: () => true },
  no_ar: { rotulo: "No ar", inclui: (e) => e === "rodando" },
  paradas: { rotulo: "Paradas", inclui: (e) => ["parado", "instalado", "nao_instalado"].includes(e) },
  problema: { rotulo: "Com problema", inclui: (e) => ["com_problema", "indisponivel"].includes(e) },
  andamento: { rotulo: "Em andamento", inclui: (e) => EM_ANDAMENTO.has(e) },
};

async function atualizarSolucoes() {
  const [dados, docker] = await Promise.all([api("/api/solucoes"), indicadorDocker()]);
  const grade = document.getElementById("grade-solucoes");
  if (!grade) return;
  emAndamento = dados.solucoes.some((s) => EM_ANDAMENTO.has(s.status.estado));
  document.getElementById("docker-slot").replaceChildren(docker);

  const erros = document.getElementById("erros-catalogo");
  erros.replaceChildren();
  if (dados.proxy?.erro) erros.append(h("div", { class: "aviso" }, `Acesso às soluções (Caddy): ${dados.proxy.mensagem}`));
  if (dados.erros_catalogo.length) {
    erros.append(h("div", { class: "aviso" }, "Alguns manifestos não puderam ser lidos:",
      h("ul", {}, dados.erros_catalogo.map((m) => h("li", {}, m)))));
  }

  const contagem = Object.fromEntries(Object.entries(GRUPOS_ESTADO).map(([k, g]) =>
    [k, dados.solucoes.filter((s) => g.inclui(s.status.estado)).length]));
  if (!contagem[filtroSolucoes] && filtroSolucoes !== "todas") filtroSolucoes = "todas";
  document.getElementById("filtros-solucoes").replaceChildren(
    ...Object.entries(GRUPOS_ESTADO)
      .filter(([k]) => k === "todas" || contagem[k] > 0)
      .map(([k, g]) => h("button", {
        type: "button", class: "filtro", "aria-pressed": String(filtroSolucoes === k),
        onclick: () => { filtroSolucoes = k; atualizarSolucoes(); },
      }, g.rotulo, h("span", { class: "filtro-num" }, contagem[k]))));

  if (!dados.solucoes.length) {
    grade.replaceChildren(h("div", { class: "vazio" },
      h("strong", {}, "Nenhuma solução no catálogo"),
      h("p", {}, "Copie modelos/solucao/ para solucoes/<id>/, preencha o manifesto e clique em Recarregar catálogo.")));
    return;
  }
  const visiveis = dados.solucoes.filter((s) => GRUPOS_ESTADO[filtroSolucoes].inclui(s.status.estado));
  grade.replaceChildren(...visiveis.map((sol) => {
    const acoes = botoesAcao(sol, null, true);
    acoes.append(h("a", { class: "botao", href: `#/admin/solucao/${sol.id}` }, "Gerenciar"));
    const detalhe = sol.status.detalhe && sol.status.detalhe !== ROTULOS[sol.status.estado] ? sol.status.detalhe : "";
    return h("article", { class: "linha-solucao", "data-estado": sol.status.estado },
      logoSistema(sol, "medio"),
      h("div", { class: "linha-solucao-info" },
        h("h2", {}, h("a", { href: `#/admin/solucao/${sol.id}` }, sol.nome)),
        h("p", {}, sol.resumo)),
      h("div", { class: "linha-solucao-estado" },
        pilula(sol.status.estado),
        detalhe ? h("small", {}, detalhe) : null,
        h("small", {}, `${sol.categoria} · v${sol.versao}`)),
      acoes);
  }));
}

/* ----- detalhe da solução (abas) ----- */

let abaSolucao = "geral";
let abaLogs = "operacao";
let ultimosLogs = { operacao: null, aplicacao: "" };

async function telaSolucao(id) {
  const sol = await api(`/api/solucoes/${id}`);
  document.title = `${sol.nome} · Portal de Soluções LOG20`;

  const repo = sol.codigo ? `${sol.codigo.repositorio.replace(/^https:\/\//, "").replace(/\.git$/, "")} (${sol.codigo.ref})` : "";
  const vi = sol.versao_instalada;
  const instalada = vi ? `${vi.commit}: ${vi.resumo}${vi.data ? ` (${quando(Date.parse(vi.data) / 1000)})` : ""}` : "Ainda não instalada";
  const ficha = h("dl", { class: "ficha" },
    [["Endereço", h("a", { href: sol.url, target: "_blank", rel: "noopener" }, sol.url)], ["Versão", sol.versao], ["Responsável", sol.responsavel],
     ["Código", repo], ["Instalado", instalada], ["Imagem", sol.imagem], ["Porta", String(sol.porta)],
     ["Banco", sol.banco ? `${sol.banco.nome} no PostgreSQL do servidor (usuário ${sol.banco.usuario})` : ""]]
      .filter(([, v]) => v)
      .map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));

  const demo = sol.acessos_demo.length
    ? h("div", { class: "bloco" },
        h("h3", {}, "Acessos de demonstração"),
        h("table", { class: "tabela-simples" },
          h("thead", {}, h("tr", {}, h("th", {}, "Perfil"), h("th", {}, "Usuário"), h("th", {}, "Senha"))),
          h("tbody", {}, sol.acessos_demo.map((a) => h("tr", {}, h("td", {}, a.perfil), h("td", {}, h("code", {}, a.usuario)), h("td", {}, h("code", {}, a.senha)))))),
        h("p", { class: "nota" }, "Login próprio da solução, para testes. Troque ou desative esses acessos antes de usar com dados reais."))
    : null;

  const formConfig = h("form", { class: "config" });
  montarFormulario(formConfig, sol);

  const formEndereco = h("form", { class: "config" },
    h("div", { class: "campo" },
      h("label", { for: "url-publica" }, "Endereço público"),
      h("input", { id: "url-publica", name: "url_publica", type: "text", value: sol.url_publica, placeholder: sol.url_padrao, autocomplete: "off" }),
      h("small", {}, `Em branco usa ${sol.url_padrao}. Com domínio próprio, informe por exemplo https://rh.suaempresa.com.br.`)),
    h("div", { class: "linha-botoes" }, h("button", { type: "submit", class: "botao" }, "Salvar endereço")));
  formEndereco.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      const r = await api(`/api/solucoes/${sol.id}/endereco`, { method: "PUT", body: { url_publica: formEndereco.url_publica.value } });
      toast(r.aviso);
    } catch (e) { toast(e.message, "erro"); }
  });

  const pessoas = h("div", { class: "bloco", id: "pessoas-solucao" }, h("p", { class: "nota" }, "Carregando…"));

  const paineis = {
    geral: h("div", { class: "aba-painel colunas" },
      h("section", { class: "painel" }, h("h2", {}, "Sobre"), h("p", { class: "descricao" }, sol.descricao || sol.resumo), demo),
      h("section", { class: "painel" }, h("h2", {}, "Ficha técnica"), ficha)),
    config: h("div", { class: "aba-painel" },
      h("section", { class: "painel estreito" }, h("h2", {}, "Configurações"),
        h("p", { class: "nota topo" }, "Viram variáveis de ambiente da solução. As mudanças valem depois de Reiniciar."), formConfig)),
    acesso: h("div", { class: "aba-painel colunas" },
      h("section", { class: "painel" }, h("h2", {}, "Quem acessa"), pessoas),
      h("section", { class: "painel" }, h("h2", {}, "Endereço"), formEndereco)),
    logs: h("div", { class: "aba-painel" },
      h("section", { class: "painel logs" },
        h("div", { class: "logs-topo" },
          h("div", { class: "abas-mini", role: "tablist", "aria-label": "Tipo de log" },
            [["operacao", "Operação do portal"], ["aplicacao", "Saída da aplicação"]].map(([aba, rotulo]) =>
              h("button", { type: "button", role: "tab", "data-aba": aba, onclick: () => { abaLogs = aba; pintarLogs(true); } }, rotulo))),
          h("small", { class: "nota" }, "Atualiza sozinho enquanto a página está aberta.")),
        h("pre", { class: "saida", tabindex: "0" }, "Carregando…"))),
  };

  const abas = [["geral", "Visão geral"], ["config", "Configurações"], ["acesso", "Acesso"], ["logs", "Logs"]];
  const barra = h("div", { class: "abas", role: "tablist" }, abas.map(([k, rotulo]) =>
    h("button", { type: "button", role: "tab", id: `aba-${k}`, "aria-controls": `painel-${k}`, onclick: () => mostrarAba(k) }, rotulo)));
  for (const [k, p] of Object.entries(paineis)) { p.id = `painel-${k}`; p.setAttribute("role", "tabpanel"); p.setAttribute("aria-labelledby", `aba-${k}`); }

  $conteudo.replaceChildren(
    h("a", { class: "voltar", href: "#/admin/solucoes" }, "Todas as soluções"),
    h("section", { class: "cabecalho" },
      logoSistema(sol, "grande"),
      h("div", { class: "cabecalho-texto" },
        h("h1", {}, sol.nome),
        h("p", {}, `${sol.categoria} · ${sol.resumo}`)),
      h("div", { class: "estado", id: "estado" })),
    h("div", { id: "acoes" }),
    barra,
    ...Object.values(paineis));

  ultimosLogs = { operacao: null, aplicacao: "" };
  abaLogs = sol.status.estado === "rodando" ? "aplicacao" : "operacao";
  mostrarAba(abaSolucao);
  pintarStatusSolucao(sol);
  carregarPessoas(sol);
  await atualizarLogs(id);
}

function mostrarAba(k) {
  abaSolucao = k;
  for (const b of $conteudo.querySelectorAll(".abas > button")) b.setAttribute("aria-selected", String(b.id === `aba-${k}`));
  for (const p of $conteudo.querySelectorAll(".aba-painel")) p.hidden = p.id !== `painel-${k}`;
  if (k === "logs") pintarLogs(true);
}

async function carregarPessoas(sol) {
  const alvo = document.getElementById("pessoas-solucao");
  try {
    const { usuarios } = await api("/api/usuarios");
    const comAcesso = usuarios.filter((u) => u.ativo && u.perfil !== "admin" && u.solucoes.includes(sol.id));
    const admins = usuarios.filter((u) => u.ativo && u.perfil === "admin").length;
    alvo.replaceChildren(
      h("p", { class: "numero-grande" }, comAcesso.length, h("span", {}, comAcesso.length === 1 ? "pessoa com acesso" : "pessoas com acesso")),
      h("p", { class: "nota" }, `Além de ${plural(admins, "administrador", "administradores")}, que acessam todos os sistemas.`),
      comAcesso.length
        ? h("ul", { class: "pessoas" }, comAcesso.slice(0, 12).map((u) => h("li", {}, avatar(u, "pequeno"), u.nome)),
            comAcesso.length > 12 ? h("li", { class: "nota" }, `e mais ${comAcesso.length - 12}`) : null)
        : null,
      h("a", { class: "botao", href: "#/admin/acessos" }, "Gerenciar acessos"));
  } catch (e) { alvo.replaceChildren(h("p", { class: "nota" }, e.message)); }
}

function montarFormulario(form, sol) {
  form.replaceChildren();
  if (!sol.configuracoes.length) {
    form.append(h("p", { class: "nota" }, "Esta solução não tem configurações."));
    return;
  }
  for (const c of sol.configuracoes) {
    const idCampo = `cfg-${c.nome}`;
    const limpar = c.tipo === "segredo" && c.definido
      ? h("button", {
          type: "button", class: "link-limpar",
          onclick: async () => {
            if (!(await confirmar({ titulo: "Apagar o valor salvo?", texto: `${c.rotulo}: o valor guardado no portal será apagado.`, botao: "Apagar valor", perigo: true }))) return;
            await api(`/api/solucoes/${sol.id}/configuracoes/${c.nome}`, { method: "DELETE" });
            montarFormulario(form, await api(`/api/solucoes/${sol.id}`));
            toast("Valor apagado. Reinicie a solução para aplicar.");
          },
        }, "Apagar valor salvo")
      : null;
    const ajuda = c.ajuda || limpar ? h("small", {}, c.ajuda ? `${c.ajuda} ` : "", limpar) : null;
    if (c.tipo === "booleano") {
      form.append(h("div", { class: "campo campo-check" },
        h("input", { id: idCampo, name: c.nome, type: "checkbox", checked: c.valor === "true" }),
        h("label", { for: idCampo }, c.rotulo), ajuda));
    } else {
      form.append(h("div", { class: "campo" },
        h("label", { for: idCampo }, c.rotulo,
          c.tipo === "segredo" ? h("span", { class: "etiqueta" }, c.definido ? "Definido" : "Vazio") : null,
          c.obrigatoria && !c.definido ? h("span", { class: "etiqueta obrigatoria" }, "Obrigatório") : null),
        h("input", {
          id: idCampo, name: c.nome, autocomplete: "off",
          type: c.tipo === "segredo" ? "password" : "text",
          value: c.tipo === "segredo" ? "" : (c.valor ?? ""),
          placeholder: c.tipo === "segredo" ? (c.definido ? "Deixe em branco para manter o valor salvo" : "Não definido") : null,
        }), ajuda));
    }
  }
  form.append(h("div", { class: "linha-botoes" }, h("button", { type: "submit", class: "botao primario" }, "Salvar configurações")));
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
      toast(`Configurações salvas. ${r.aviso}`);
    } catch (e) { toast(e.message, "erro"); }
  };
}

function pintarStatusSolucao(sol) {
  emAndamento = EM_ANDAMENTO.has(sol.status.estado);
  const estado = document.getElementById("estado");
  if (!estado) return;
  const detalhe = sol.status.detalhe === ROTULOS[sol.status.estado] ? "" : sol.status.detalhe;
  estado.replaceChildren(pilula(sol.status.estado), detalhe ? h("small", {}, detalhe) : null);
  document.getElementById("acoes").replaceChildren(botoesAcao(sol, () => { abaLogs = "operacao"; mostrarAba("logs"); }));
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
  for (const b of $conteudo.querySelectorAll(".abas-mini button")) b.setAttribute("aria-selected", String(b.dataset.aba === abaLogs));
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

const estadoUsuarios = { dados: null, busca: "", filtro: "todos", selecionados: new Set() };

const FILTROS_USUARIO = {
  todos: { rotulo: "Todos", inclui: () => true },
  ativos: { rotulo: "Ativos", inclui: (u) => u.ativo },
  temporaria: { rotulo: "Aguardando 1º acesso", inclui: (u) => u.ativo && u.trocar_senha },
  admins: { rotulo: "Administradores", inclui: (u) => u.perfil === "admin" },
  sem_sistema: { rotulo: "Sem sistemas", inclui: (u) => u.ativo && u.perfil !== "admin" && !u.solucoes.length },
  desativados: { rotulo: "Desativados", inclui: (u) => !u.ativo },
};

async function carregarUsuarios() {
  estadoUsuarios.dados = await api("/api/usuarios");
  const ids = new Set(estadoUsuarios.dados.usuarios.map((u) => u.id));
  for (const id of [...estadoUsuarios.selecionados]) if (!ids.has(id)) estadoUsuarios.selecionados.delete(id);
  return estadoUsuarios.dados;
}

function situacaoUsuario(u) {
  if (!u.ativo) return pilula("parado", "Desativado");
  if (u.trocar_senha) return pilula("iniciando", "Aguardando 1º acesso");
  return pilula("rodando", "Ativo");
}

function chipsSistemas(u, nomes) {
  if (u.perfil === "admin") return h("span", { class: "nota" }, "Todos (administrador)");
  if (!u.solucoes.length) return h("span", { class: "nota" }, "Nenhum");
  const mostrar = u.solucoes.slice(0, 2);
  return h("span", { class: "chips" },
    mostrar.map((s) => h("span", { class: "chip" }, nomes[s] || s)),
    u.solucoes.length > 2 ? h("span", { class: "chip mais", title: u.solucoes.slice(2).map((s) => nomes[s] || s).join(", ") }, `+${u.solucoes.length - 2}`) : null);
}

async function telaUsuarios() {
  await carregarUsuarios();
  const busca = h("input", {
    type: "search", class: "busca", placeholder: "Procurar por nome ou login", "aria-label": "Procurar usuário",
    value: estadoUsuarios.busca,
    oninput: (ev) => { estadoUsuarios.busca = ev.target.value; pintarUsuarios(); },
  });
  $conteudo.replaceChildren(
    h("section", { class: "intro intro-linha" },
      h("div", {}, h("h1", {}, "Usuários"), h("p", {}, "Quem pode entrar no portal. Para escolher quem vê cada sistema, use também Acessos.")),
      h("div", { class: "intro-lado" },
        h("button", { type: "button", class: "botao", onclick: dialogoImportar }, "Importar em lote"),
        h("button", { type: "button", class: "botao primario", onclick: () => dialogoUsuario(null) }, "Novo usuário"))),
    h("div", { class: "barra-ferramentas" }, busca, h("div", { class: "filtros", id: "filtros-usuarios", role: "group", "aria-label": "Filtrar usuários" })),
    h("div", { class: "painel tabela-rolagem" },
      h("table", { class: "tabela" },
        h("thead", {}, h("tr", {},
          h("th", { class: "col-check" }, h("input", { type: "checkbox", id: "marcar-todos", "aria-label": "Selecionar todos os usuários listados", onchange: (ev) => marcarTodos(ev.target.checked) })),
          ["Pessoa", "Sistemas", "Último acesso", "Situação", ""].map((t) => h("th", {}, t)))),
        h("tbody", { id: "corpo-usuarios" }))),
    h("div", { class: "barra-selecao", id: "barra-selecao", hidden: true }));
  pintarUsuarios();
}

function usuariosVisiveis() {
  const { dados, busca, filtro } = estadoUsuarios;
  const termo = normalizar(busca);
  return dados.usuarios.filter((u) => FILTROS_USUARIO[filtro].inclui(u)
    && (!termo || normalizar(`${u.nome} ${u.usuario}`).includes(termo)));
}

function pintarUsuarios() {
  const { dados, selecionados } = estadoUsuarios;
  const corpo = document.getElementById("corpo-usuarios");
  if (!corpo) return;
  const nomes = Object.fromEntries(dados.solucoes.map((s) => [s.id, s.nome]));

  document.getElementById("filtros-usuarios").replaceChildren(...Object.entries(FILTROS_USUARIO)
    .map(([k, f]) => [k, f, dados.usuarios.filter(f.inclui).length])
    .filter(([k, , n]) => k === "todos" || n > 0 || estadoUsuarios.filtro === k)
    .map(([k, f, n]) => h("button", {
      type: "button", class: "filtro", "aria-pressed": String(estadoUsuarios.filtro === k),
      onclick: () => { estadoUsuarios.filtro = k; pintarUsuarios(); },
    }, f.rotulo, h("span", { class: "filtro-num" }, n))));

  const lista = usuariosVisiveis();
  corpo.replaceChildren(...lista.map((u) => h("tr", { class: [u.ativo ? "" : "inativo", selecionados.has(u.id) ? "marcada" : ""].join(" ").trim() || null },
    h("td", { class: "col-check" }, h("input", {
      type: "checkbox", checked: selecionados.has(u.id), "aria-label": `Selecionar ${u.nome}`,
      onchange: (ev) => { ev.target.checked ? selecionados.add(u.id) : selecionados.delete(u.id); pintarUsuarios(); },
    })),
    h("td", {}, h("div", { class: "pessoa" }, avatar(u),
      h("div", {}, h("button", { type: "button", class: "link-nome", onclick: () => dialogoUsuario(u) }, u.nome),
        h("div", { class: "sub" }, u.usuario, u.perfil === "admin" ? h("span", { class: "etiqueta admin" }, "Administrador") : null)))),
    h("td", {}, chipsSistemas(u, nomes)),
    h("td", { class: "nowrap" }, quando(u.ultimo_login)),
    h("td", {}, situacaoUsuario(u)),
    h("td", { class: "col-acoes" }, h("button", { type: "button", class: "botao pequeno", onclick: () => dialogoUsuario(u) }, "Editar")))));
  if (!lista.length) {
    corpo.append(h("tr", {}, h("td", { colspan: "6", class: "vazio-linha" },
      dados.usuarios.length ? "Ninguém encontrado com esse filtro." : "Nenhum usuário cadastrado.")));
  }
  const todos = document.getElementById("marcar-todos");
  const marcadosVisiveis = lista.filter((u) => selecionados.has(u.id)).length;
  todos.checked = lista.length > 0 && marcadosVisiveis === lista.length;
  todos.indeterminate = marcadosVisiveis > 0 && marcadosVisiveis < lista.length;
  pintarBarraSelecao();
}

function marcarTodos(marcar) {
  for (const u of usuariosVisiveis()) marcar ? estadoUsuarios.selecionados.add(u.id) : estadoUsuarios.selecionados.delete(u.id);
  pintarUsuarios();
}

function pintarBarraSelecao() {
  const barra = document.getElementById("barra-selecao");
  const ids = [...estadoUsuarios.selecionados];
  barra.hidden = !ids.length;
  if (!ids.length) return;
  const sel = estadoUsuarios.dados.usuarios.filter((u) => estadoUsuarios.selecionados.has(u.id));
  const algumAtivo = sel.some((u) => u.ativo);
  const algumInativo = sel.some((u) => !u.ativo);
  barra.replaceChildren(
    h("strong", {}, plural(ids.length, "selecionado", "selecionados")),
    h("div", { class: "barra-selecao-acoes" },
      h("button", { type: "button", class: "botao pequeno", onclick: () => dialogoAcessoEmMassa(sel, true) }, "Liberar sistemas"),
      h("button", { type: "button", class: "botao pequeno", onclick: () => dialogoAcessoEmMassa(sel, false) }, "Retirar sistemas"),
      algumInativo ? h("button", { type: "button", class: "botao pequeno", onclick: () => mudarSituacao(sel, true) }, "Reativar") : null,
      algumAtivo ? h("button", { type: "button", class: "botao pequeno perigo", onclick: () => mudarSituacao(sel, false) }, "Desativar") : null,
      h("button", { type: "button", class: "botao pequeno sutil", onclick: () => { estadoUsuarios.selecionados.clear(); pintarUsuarios(); } }, "Limpar seleção")));
}

function dialogoAcessoEmMassa(sel, conceder) {
  const comuns = sel.filter((u) => u.perfil !== "admin");
  const escolha = escolhaSistemas(estadoUsuarios.dados.solucoes);
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const verbo = conceder ? "Liberar" : "Retirar";
  const dlg = dialogo({
    titulo: `${verbo} sistemas`,
    descricao: `${conceder ? "Para" : "De"} ${plural(comuns.length, "pessoa selecionada", "pessoas selecionadas")}.`
      + (comuns.length < sel.length ? ` Administradores ficam de fora: eles já veem todos os sistemas.` : ""),
    corpo: [escolha.el, erro],
    rodape: [
      h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"),
      h("button", {
        type: "button", class: `botao ${conceder ? "primario" : "perigo-cheio"}`, disabled: !comuns.length,
        onclick: async (ev) => {
          const solucoes = escolha.marcados();
          if (!solucoes.length) { erro.textContent = "Marque pelo menos um sistema."; erro.hidden = false; return; }
          ev.currentTarget.disabled = true;
          try {
            const r = await api("/api/acessos", { method: "POST", body: { usuarios: comuns.map((u) => u.id), solucoes, conceder } });
            dlg.close();
            toast(r.alterados ? `${conceder ? "Acesso liberado" : "Acesso retirado"}: ${plural(r.alterados, "permissão alterada", "permissões alteradas")}.` : "Nada mudou: as pessoas já estavam assim.");
            await carregarUsuarios(); pintarUsuarios();
          } catch (e) { erro.textContent = e.message; erro.hidden = false; ev.currentTarget.disabled = false; }
        },
      }, `${verbo} acesso`),
    ],
  });
}

async function mudarSituacao(sel, ativo) {
  const alvos = sel.filter((u) => u.ativo !== ativo);
  if (!ativo && !(await confirmar({
    titulo: `Desativar ${plural(alvos.length, "usuário", "usuários")}?`,
    texto: "Essas pessoas saem do portal na hora e não conseguem entrar de novo até serem reativadas. Os acessos liberados ficam guardados.",
    botao: "Desativar", perigo: true,
  }))) return;
  try {
    const r = await api("/api/usuarios/situacao", { method: "POST", body: { usuarios: alvos.map((u) => u.id), ativo } });
    if (r.erros.length) toast(`${r.alterados} alterado(s). Não foi possível: ${r.erros.join("; ")}`, "erro");
    else toast(`${plural(r.alterados, "usuário", "usuários")} ${ativo ? "reativado(s)" : "desativado(s)"}.`);
    await carregarUsuarios(); pintarUsuarios();
  } catch (e) { toast(e.message, "erro"); }
}

/* ----- novo / editar usuário ----- */

function dialogoUsuario(u) {
  const novo = !u;
  const { solucoes } = estadoUsuarios.dados;
  const escolha = escolhaSistemas(solucoes, new Set(u?.solucoes || []));
  const notaAdmin = h("p", { class: "nota", hidden: true }, "Administradores acessam todos os sistemas e gerenciam o portal.");
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const form = h("form", { class: "form-usuario", id: "form-usuario" },
    h("div", { class: "form-grade" },
      h("div", { class: "campo" }, h("label", { for: "u-nome" }, "Nome"),
        h("input", { id: "u-nome", name: "nome", type: "text", value: u?.nome || "", required: true, autocomplete: "off", maxlength: 120 })),
      h("div", { class: "campo" }, h("label", { for: "u-usuario" }, "Login"),
        h("input", { id: "u-usuario", name: "usuario", type: "text", value: u?.usuario || "", required: true, disabled: !novo, autocomplete: "off", pattern: "[a-zA-Z0-9._@\\-]{3,60}" }),
        h("small", {}, novo ? "Ex.: maria.silva ou a matrícula. Não muda depois." : "O login não pode ser alterado."))),
    h("fieldset", { class: "grupo-campos" },
      h("legend", {}, "Perfil"),
      h("div", { class: "opcoes-perfil" },
        [["usuario", "Usuário", "Vê só os sistemas liberados para ele."], ["admin", "Administrador", "Vê tudo e gerencia o portal."]].map(([v, rotulo, ajuda]) =>
          h("label", { class: "opcao-perfil" },
            h("input", { type: "radio", name: "perfil", value: v, checked: (u?.perfil || "usuario") === v }),
            h("span", {}, h("strong", {}, rotulo), h("small", {}, ajuda)))))),
    h("fieldset", { class: "grupo-campos", id: "grupo-sistemas" }, h("legend", {}, "Sistemas liberados"), escolha.el, notaAdmin),
    novo ? null : h("div", { class: "campo campo-check" },
      h("input", { id: "u-ativo", name: "ativo", type: "checkbox", checked: u.ativo, disabled: u.id === eu.id }),
      h("label", { for: "u-ativo" }, "Pode entrar no portal"),
      h("small", {}, u.id === eu.id ? "Você não pode desativar a própria conta." : "Desmarque para bloquear o acesso sem apagar a pessoa.")),
    erro);

  const sincronizar = () => {
    const admin = form.perfil.value === "admin";
    for (const c of escolha.el.querySelectorAll("input")) c.disabled = admin;
    escolha.el.classList.toggle("desligado", admin);
    notaAdmin.hidden = !admin;
  };
  form.addEventListener("change", (ev) => { if (ev.target.name === "perfil") sincronizar(); });
  sincronizar();

  const redefinir = novo || u.id === eu.id ? null : h("button", {
    type: "button", class: "botao sutil", onclick: async () => {
      if (!(await confirmar({ titulo: `Redefinir a senha de ${u.nome}?`, texto: "A senha atual deixa de valer e a pessoa sai do portal. Você recebe uma senha temporária para passar a ela.", botao: "Redefinir senha" }))) return;
      try {
        const r = await api(`/api/usuarios/${u.id}/redefinir-senha`, { method: "POST" });
        dlg.close();
        dialogo({
          titulo: "Senha redefinida",
          corpo: painelSenha(`Nova senha temporária de ${u.nome}`, r.senha_temporaria),
          rodape: h("button", { type: "button", class: "botao primario", onclick: (ev) => ev.target.closest("dialog").close() }, "Concluir"),
          aoFechar: async () => { await carregarUsuarios(); pintarUsuarios(); },
        });
      } catch (e) { toast(e.message, "erro"); }
    },
  }, "Redefinir senha");

  const dlg = dialogo({
    titulo: novo ? "Novo usuário" : u.nome,
    descricao: novo ? "A pessoa recebe uma senha temporária e cria a própria senha no primeiro acesso." : `Criado em ${quando(u.criado_em, true)} · último acesso: ${quando(u.ultimo_login)}`,
    corpo: form,
    rodape: [
      redefinir, h("span", { class: "espaco" }),
      h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"),
      h("button", { type: "submit", form: "form-usuario", class: "botao primario" }, novo ? "Criar usuário" : "Salvar alterações"),
    ],
  });
  form.querySelector("input:not([disabled])")?.focus();

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    erro.hidden = true;
    const perfil = form.perfil.value;
    const solucoesSel = perfil === "admin" ? [] : escolha.marcados();
    try {
      if (novo) {
        const r = await api("/api/usuarios", { method: "POST", body: { usuario: form.usuario.value.trim(), nome: form.nome.value, perfil, solucoes: solucoesSel } });
        dlg.close();
        dialogo({
          titulo: "Usuário criado",
          corpo: painelSenha(`Senha temporária de ${r.usuario.nome} (login ${r.usuario.usuario})`, r.senha_temporaria),
          rodape: h("button", { type: "button", class: "botao primario", onclick: (e) => e.target.closest("dialog").close() }, "Concluir"),
          aoFechar: async () => { await carregarUsuarios(); pintarUsuarios(); },
        });
      } else {
        await api(`/api/usuarios/${u.id}`, { method: "PATCH", body: { nome: form.nome.value, perfil, ativo: form.ativo.checked, solucoes: solucoesSel } });
        dlg.close();
        toast("Alterações salvas.");
        if (u.id === eu.id) { eu = await api("/api/auth/eu"); montarTopo(); }
        await carregarUsuarios(); pintarUsuarios();
      }
    } catch (e) { erro.textContent = e.message; erro.hidden = false; }
  });
}

/* ----- importar em lote ----- */

/** Lê "login; nome; perfil" por linha. Aceita ; , ou tabulação (colar do Excel). */
function lerLinhasLote(texto) {
  const linhas = texto.replace(/\r/g, "").split("\n").map((l) => l.trim()).filter(Boolean);
  const itens = linhas.map((linha) => {
    const sep = linha.includes("\t") ? "\t" : linha.includes(";") ? ";" : ",";
    const campos = linha.split(sep).map((c) => c.trim().replace(/^"(.*)"$/, "$1").trim());
    return { usuario: campos[0] || "", nome: (campos[1] || "").replace(/\s+/g, " "), perfilTexto: campos[2] || "" };
  });
  if (itens.length && ["usuario", "login", "matricula", "user"].includes(normalizar(itens[0].usuario))) itens.shift();
  return itens;
}

function validarLote(itens, existentes) {
  const vistos = new Map();
  return itens.map((it, i) => {
    const perfil = PERFIL_TEXTO[normalizar(it.perfilTexto)] ?? PERFIL_TEXTO[it.perfilTexto.toLowerCase()];
    let erro = "";
    const chave = it.usuario.toLowerCase();
    if (!USUARIO_RE.test(it.usuario)) erro = "Login inválido: 3 a 60 letras, números, ponto, hífen, _ ou @.";
    else if (!it.nome) erro = "Falta o nome.";
    else if (!perfil) erro = `Perfil "${it.perfilTexto}" não reconhecido: use usuário ou administrador.`;
    else if (existentes.has(chave)) erro = "Já existe um usuário com este login.";
    else if (vistos.has(chave)) erro = `Repetido (linha ${vistos.get(chave)}).`;
    if (!vistos.has(chave)) vistos.set(chave, i + 1);
    return { ...it, linha: i + 1, perfil: perfil || "usuario", erro };
  });
}

function dialogoImportar() {
  const { usuarios, solucoes } = estadoUsuarios.dados;
  const existentes = new Set(usuarios.map((u) => u.usuario.toLowerCase()));
  let itens = [];
  let errosServidor = new Map();

  const texto = h("textarea", {
    id: "lote-texto", rows: 7, spellcheck: false, "aria-describedby": "lote-ajuda",
    placeholder: "maria.silva; Maria Silva\njoao.souza; João Souza; administrador\n10452; Ana Paula Lima",
  });
  const arquivo = h("input", { type: "file", accept: ".csv,.txt,text/csv,text/plain", id: "lote-arquivo", class: "visualmente-oculto" });
  const escolha = escolhaSistemas(solucoes);
  const resumo = h("p", { class: "lote-resumo", "aria-live": "polite" });
  const previa = h("div", { class: "lote-previa" });
  const criar = h("button", { type: "button", class: "botao primario", disabled: true }, "Criar usuários");
  const erroGeral = h("p", { class: "form-erro", role: "alert", hidden: true });

  const repintar = () => {
    errosServidor = new Map();
    itens = validarLote(lerLinhasLote(texto.value), existentes);
    pintarPrevia();
  };
  const pintarPrevia = () => {
    const comErro = itens.filter((i) => i.erro || errosServidor.has(i.linha));
    const prontos = itens.length - comErro.length;
    criar.disabled = !itens.length || comErro.length > 0;
    criar.textContent = itens.length && !comErro.length ? `Criar ${plural(prontos, "usuário", "usuários")}` : "Criar usuários";
    resumo.replaceChildren(!itens.length ? "Cole a lista ou escolha um arquivo para ver a prévia."
      : comErro.length ? h("span", { class: "texto-erro" }, `${plural(comErro.length, "linha precisa", "linhas precisam")} de ajuste. Corrija no texto acima; nada é criado enquanto houver erro.`)
        : `${plural(prontos, "pessoa pronta", "pessoas prontas")} para criar.`);
    previa.replaceChildren(itens.length ? h("table", { class: "tabela-simples" },
      h("thead", {}, h("tr", {}, ["Linha", "Login", "Nome", "Perfil", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, itens.map((i) => {
        const erro = i.erro || errosServidor.get(i.linha);
        return h("tr", { class: erro ? "linha-erro" : "" },
          h("td", { class: "sub" }, i.linha), h("td", {}, h("code", {}, i.usuario || "—")), h("td", {}, i.nome || "—"),
          h("td", {}, i.perfil === "admin" ? "Administrador" : "Usuário"),
          h("td", {}, erro ? h("span", { class: "texto-erro" }, erro) : h("span", { class: "texto-ok" }, "Ok")));
      }))) : null);
  };
  texto.addEventListener("input", repintar);
  arquivo.addEventListener("change", async () => {
    const f = arquivo.files[0];
    if (!f) return;
    if (f.size > 1024 * 1024) { toast("Arquivo grande demais (máximo 1 MB).", "erro"); return; }
    const bytes = await f.arrayBuffer();
    let conteudo = new TextDecoder("utf-8").decode(bytes);
    if (conteudo.includes("�")) conteudo = new TextDecoder("windows-1252").decode(bytes); // CSV salvo pelo Excel
    texto.value = conteudo.replace(/^﻿/, "");
    arquivo.value = "";
    repintar();
  });

  const corpo = h("div", { class: "lote" },
    h("div", { class: "lote-entrada" },
      h("div", { class: "campo" },
        h("label", { for: "lote-texto" }, "Lista de pessoas"),
        texto,
        h("small", { id: "lote-ajuda" }, "Uma pessoa por linha: login; nome; perfil. O perfil é opcional (padrão: usuário). "
          + "Pode colar direto do Excel, com as colunas nessa ordem.")),
      h("div", { class: "linha-botoes" },
        h("label", { for: "lote-arquivo", class: "botao pequeno" }, "Abrir arquivo CSV"), arquivo,
        h("button", { type: "button", class: "botao pequeno sutil", onclick: () => baixarCsv("modelo-usuarios.csv", [["login", "nome", "perfil"], ["maria.silva", "Maria Silva", "usuario"], ["10452", "Ana Paula Lima", "usuario"]]) }, "Baixar modelo"))),
    h("fieldset", { class: "grupo-campos" }, h("legend", {}, "Liberar para todos os importados"), escolha.el,
      h("p", { class: "nota" }, "Opcional. Administradores da lista já veem tudo.")),
    h("div", {}, resumo, previa),
    erroGeral);

  const dlg = dialogo({
    titulo: "Importar usuários em lote",
    descricao: "Cada pessoa recebe uma senha temporária. Você baixa a lista com as senhas no fim.",
    corpo, largo: true,
    rodape: [h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"), criar],
  });
  texto.focus();
  pintarPrevia();

  criar.addEventListener("click", async () => {
    erroGeral.hidden = true;
    criar.disabled = true;
    criar.textContent = "Criando…";
    try {
      const r = await api("/api/usuarios/lote", {
        method: "POST",
        body: { usuarios: itens.map((i) => ({ usuario: i.usuario, nome: i.nome, perfil: i.perfil })), solucoes: escolha.marcados() },
      });
      dlg.close();
      dialogoResultadoLote(r.criados, escolha.marcados());
    } catch (e) {
      errosServidor = new Map((e.dados?.problemas || []).map((p) => [p.linha, p.erro]));
      erroGeral.textContent = e.message;
      erroGeral.hidden = false;
      pintarPrevia();
    }
  });
}

function dialogoResultadoLote(criados, solucoes) {
  const nomesSol = estadoUsuarios.dados.solucoes.filter((s) => solucoes.includes(s.id)).map((s) => s.nome);
  const linhas = [["login", "nome", "senha_temporaria"], ...criados.map((c) => [c.usuario, c.nome, c.senha_temporaria])];
  const textoCopia = criados.map((c) => `${c.usuario}\t${c.nome}\t${c.senha_temporaria}`).join("\n");
  let baixou = false;
  const dlg = dialogo({
    titulo: `${plural(criados.length, "usuário criado", "usuários criados")}`,
    descricao: nomesSol.length ? `Com acesso a ${nomesSol.join(", ")}.` : "Sem sistemas liberados ainda: use Acessos para liberar.",
    largo: true,
    corpo: [
      h("div", { class: "senha-temp" },
        h("strong", {}, "Guarde as senhas agora"),
        h("small", {}, "Elas não serão mostradas de novo. Entregue cada senha só à pessoa dona do login e apague o arquivo depois. No primeiro acesso cada um cria a própria senha.")),
      h("div", { class: "tabela-rolagem limite" }, h("table", { class: "tabela-simples" },
        h("thead", {}, h("tr", {}, h("th", {}, "Login"), h("th", {}, "Nome"), h("th", {}, "Senha temporária"))),
        h("tbody", {}, criados.map((c) => h("tr", {}, h("td", {}, h("code", {}, c.usuario)), h("td", {}, c.nome), h("td", {}, h("code", { class: "senha" }, c.senha_temporaria))))))),
    ],
    rodape: [
      h("button", { type: "button", class: "botao", onclick: (ev) => copiar(textoCopia, ev.currentTarget) }, "Copiar tudo"),
      h("button", { type: "button", class: "botao", onclick: () => { baixou = true; baixarCsv(`usuarios-criados-${hojeIso()}.csv`, linhas); } }, "Baixar planilha"),
      h("span", { class: "espaco" }),
      h("button", {
        type: "button", class: "botao primario",
        onclick: async () => {
          if (!baixou && !(await confirmar({ titulo: "Fechar sem baixar as senhas?", texto: "Depois de fechar, só dá para gerar senhas novas com Redefinir senha, uma pessoa por vez.", botao: "Fechar mesmo assim" }))) return;
          dlg.close();
        },
      }, "Concluir"),
    ],
    aoFechar: async () => { await carregarUsuarios(); pintarUsuarios(); },
  });
  // Esc também passa pela confirmação.
  dlg.addEventListener("cancel", (ev) => { if (!baixou) { ev.preventDefault(); dlg.querySelector(".dialogo-rodape .primario").click(); } });
}

/* ================= admin: acessos ================= */

const estadoAcessos = { aba: "matriz", busca: "", inativos: false, buscaHist: "" };

async function telaAcessos() {
  const abas = [["matriz", "Quem acessa o quê"], ["historico", "Histórico de alterações"]];
  $conteudo.replaceChildren(
    h("section", { class: "intro" },
      h("h1", {}, "Acessos"),
      h("p", {}, "Marque quem pode abrir cada sistema. A mudança vale na hora, sem precisar salvar.")),
    h("div", { class: "abas", role: "tablist" }, abas.map(([k, rotulo]) =>
      h("button", { type: "button", role: "tab", "aria-selected": String(estadoAcessos.aba === k), onclick: () => { estadoAcessos.aba = k; telaAcessos(); } }, rotulo))),
    h("div", { id: "acessos-corpo" }, h("p", { class: "nota" }, "Carregando…")));
  if (estadoAcessos.aba === "historico") await pintarHistorico();
  else await pintarMatriz();
}

async function pintarMatriz() {
  const { usuarios, solucoes } = await carregarUsuarios();
  const alvo = document.getElementById("acessos-corpo");
  if (!alvo) return;
  if (!solucoes.length) {
    alvo.replaceChildren(h("div", { class: "vazio" }, h("strong", {}, "Nenhuma solução no catálogo"), h("p", {}, "Publique uma solução em Soluções para liberar acesso a ela.")));
    return;
  }
  const busca = h("input", {
    type: "search", class: "busca", placeholder: "Procurar pessoa", "aria-label": "Procurar pessoa", value: estadoAcessos.busca,
    oninput: (ev) => { estadoAcessos.busca = ev.target.value; pintarLinhas(); },
  });
  const inativos = h("label", { class: "campo-check compacto" },
    h("input", { type: "checkbox", checked: estadoAcessos.inativos, onchange: (ev) => { estadoAcessos.inativos = ev.target.checked; pintarLinhas(); } }),
    h("span", {}, "Mostrar desativados"));
  const corpo = h("tbody");
  const contadores = new Map();

  const visiveis = () => {
    const termo = normalizar(estadoAcessos.busca);
    return usuarios.filter((u) => (estadoAcessos.inativos || u.ativo) && (!termo || normalizar(`${u.nome} ${u.usuario}`).includes(termo)))
      .sort((a, b) => (a.perfil === "admin") - (b.perfil === "admin") || a.nome.localeCompare(b.nome, "pt-BR"));
  };
  const atualizarContadores = () => {
    for (const s of solucoes) {
      const n = usuarios.filter((u) => u.ativo && u.perfil !== "admin" && u.solucoes.includes(s.id)).length;
      contadores.get(s.id).textContent = plural(n, "pessoa", "pessoas");
    }
  };
  const alternar = async (u, s, marcar, caixa) => {
    caixa.disabled = true;
    try {
      await api("/api/acessos", { method: "POST", body: { usuarios: [u.id], solucoes: [s.id], conceder: marcar } });
      if (marcar) u.solucoes.push(s.id); else u.solucoes = u.solucoes.filter((x) => x !== s.id);
      atualizarContadores();
    } catch (e) { caixa.checked = !marcar; toast(e.message, "erro"); }
    caixa.disabled = false;
  };
  const emColuna = async (s, conceder) => {
    const alvos = visiveis().filter((u) => u.ativo && u.perfil !== "admin" && u.solucoes.includes(s.id) !== conceder);
    if (!alvos.length) { toast(conceder ? "Todas as pessoas listadas já têm acesso." : "Ninguém listado tem acesso."); return; }
    if (!(await confirmar({
      titulo: `${conceder ? "Liberar" : "Retirar"} ${s.nome}?`,
      texto: `${conceder ? "Liberar para" : "Retirar de"} ${plural(alvos.length, "pessoa listada", "pessoas listadas")}`
        + (estadoAcessos.busca ? ` (filtro: "${estadoAcessos.busca}")` : "") + ".",
      botao: conceder ? "Liberar para todos" : "Retirar de todos", perigo: !conceder,
    }))) return;
    try {
      await api("/api/acessos", { method: "POST", body: { usuarios: alvos.map((u) => u.id), solucoes: [s.id], conceder } });
      for (const u of alvos) { if (conceder) u.solucoes.push(s.id); else u.solucoes = u.solucoes.filter((x) => x !== s.id); }
      atualizarContadores(); pintarLinhas();
      toast(`${conceder ? "Acesso liberado para" : "Acesso retirado de"} ${plural(alvos.length, "pessoa", "pessoas")}.`);
    } catch (e) { toast(e.message, "erro"); }
  };

  const pintarLinhas = () => {
    const lista = visiveis();
    corpo.replaceChildren(...lista.map((u) => h("tr", { class: u.ativo ? "" : "inativo" },
      h("th", { scope: "row" }, h("div", { class: "pessoa" }, avatar(u, "pequeno"),
        h("div", {}, h("span", { class: "pessoa-nome" }, u.nome), h("div", { class: "sub" }, u.usuario)))),
      solucoes.map((s) => h("td", { class: "celula" }, u.perfil === "admin"
        ? h("span", { class: "sempre", title: "Administradores acessam todos os sistemas" }, "Todos")
        : h("input", {
            type: "checkbox", class: "marca-acesso", checked: u.solucoes.includes(s.id),
            "aria-label": `${s.nome} para ${u.nome}`,
            onchange: (ev) => alternar(u, s, ev.target.checked, ev.target),
          }))))));
    if (!lista.length) corpo.append(h("tr", {}, h("td", { colspan: String(solucoes.length + 1), class: "vazio-linha" }, "Ninguém encontrado.")));
  };

  const cabecalho = h("tr", {}, h("th", { class: "canto" }, "Pessoa"), solucoes.map((s) => {
    const cont = h("small", {});
    contadores.set(s.id, cont);
    return h("th", { class: "col-sistema", scope: "col" },
      h("div", { class: "col-sistema-topo" }, logoSistema(s, "mini"), h("span", {}, s.nome)),
      cont,
      h("div", { class: "col-sistema-acoes" },
        h("button", { type: "button", class: "link-acao", onclick: () => emColuna(s, true) }, "Liberar todos"),
        h("button", { type: "button", class: "link-acao", onclick: () => emColuna(s, false) }, "Retirar todos")));
  }));

  alvo.replaceChildren(
    h("div", { class: "barra-ferramentas" }, busca, inativos),
    h("div", { class: "painel tabela-rolagem matriz-caixa" }, h("table", { class: "matriz" }, h("thead", {}, cabecalho), corpo)),
    h("p", { class: "nota" }, "“Liberar todos” e “Retirar todos” valem para as pessoas ativas que aparecem na lista, respeitando a busca."));
  atualizarContadores();
  pintarLinhas();
}

async function pintarHistorico() {
  const { eventos } = await api("/api/historico?limite=300");
  const alvo = document.getElementById("acessos-corpo");
  if (!alvo) return;
  const lista = h("ol", { class: "historico" });
  const pintar = () => {
    const termo = normalizar(estadoAcessos.buscaHist);
    const filtrados = eventos.filter((e) => !termo || normalizar(`${e.autor} ${e.acao} ${e.detalhe}`).includes(termo));
    lista.replaceChildren(...filtrados.map((e) => h("li", {},
      h("time", { datetime: new Date(e.quando * 1000).toISOString() }, quando(e.quando)),
      h("div", {}, h("strong", {}, e.acao), h("span", { class: "sub" }, ` por ${e.autor}`), h("p", {}, e.detalhe)))));
    if (!filtrados.length) lista.append(h("li", { class: "vazio-linha" }, eventos.length ? "Nada encontrado." : "Nenhuma alteração registrada ainda."));
  };
  alvo.replaceChildren(
    h("div", { class: "barra-ferramentas" }, h("input", {
      type: "search", class: "busca", placeholder: "Procurar no histórico", "aria-label": "Procurar no histórico", value: estadoAcessos.buscaHist,
      oninput: (ev) => { estadoAcessos.buscaHist = ev.target.value; pintar(); },
    })),
    h("div", { class: "painel" }, lista),
    h("p", { class: "nota" }, "Registra criação de usuários, mudanças de acesso, perfil e situação e senhas redefinidas. Mostra as 300 alterações mais recentes."));
  pintar();
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
        if (sistema.disponivel) { location.href = alvo.href; return; }
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
    // Não atualiza com um diálogo aberto, para não mexer no que a pessoa está editando.
    if (!document.hidden && !document.querySelector("dialog[open]")) await atualizar();
    agendar();
  }, emAndamento ? ritmo[0] : ritmo[1]);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && ritmo) atualizar().then(agendar);
});

async function navegar() {
  clearTimeout(timer);
  ritmo = null;
  for (const d of document.querySelectorAll("dialog[open]")) d.close();
  const [caminho, consulta] = (location.hash || "#/").split("?");
  const mudouTela = caminho !== rota;
  rota = caminho;
  const voltar = new URLSearchParams(consulta || "").get("voltar");
  if (voltar) voltarPara = voltar;
  document.title = "Portal de Soluções LOG20";

  if (!eu) {
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
  if (mudouTela) { $conteudo.replaceChildren(); window.scrollTo(0, 0); }
  if (mudouTela && rota.startsWith("#/admin/solucao/")) abaSolucao = "geral";
  let novoRitmo = null;
  try {
    if (rota === "#/login") telaLogin();
    else if (rota === "#/senha") telaSenha();
    else if (rota === "#/admin/solucoes") { await telaSolucoes(); novoRitmo = [3000, 30000]; }
    else if (rota.startsWith("#/admin/solucao/")) { await telaSolucao(rota.split("/")[3]); novoRitmo = [2500, 20000]; }
    else if (rota === "#/admin/usuarios") await telaUsuarios();
    else if (rota === "#/admin/acessos") await telaAcessos();
    else { rota = "#/"; await telaMeusSistemas(); novoRitmo = [60000, 60000]; }
  } catch (e) {
    if (e.status === 401 || location.hash.split("?")[0] !== rota) return;
    $conteudo.replaceChildren(h("div", { class: "aviso" }, e.message), h("a", { class: "voltar", href: "#/" }, "Voltar ao início"));
  }
  ritmo = novoRitmo;
  agendar();
}

window.addEventListener("hashchange", navegar);
navegar();
