"use strict";

/* =========================================================================
   Portal de Soluções LOG20 — interface (sem frameworks)
   Rotas: #/  #/login  #/senha  #/admin/solucoes  #/admin/solucao/<id>
          #/admin/usuarios  #/admin/acessos  #/admin/backups  #/admin/uso  #/admin/alertas
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

function baixarTexto(nome, texto) {
  const url = URL.createObjectURL(new Blob([texto], { type: "text/plain;charset=utf-8" }));
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
    h("small", {}, extra || "Anote agora: ela não será mostrada de novo. No primeiro acesso o usuário cria uma senha própria."));
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
  solucoes = solucoes.filter((s) => !s.somente_admin);  // só administradores: não se libera
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
        item("#/admin/solucoes", "Soluções", rota.startsWith("#/admin/solucao") || rota === "#/admin/nova-solucao" || rota.startsWith("#/admin/editar-solucao/")),
        item("#/admin/usuarios", "Usuários", rota === "#/admin/usuarios"),
        item("#/admin/acessos", "Acessos", rota === "#/admin/acessos"),
        item("#/admin/backups", "Backups", rota === "#/admin/backups"),
        item("#/admin/uso", "Uso", rota === "#/admin/uso"),
        h("a", { href: "#/admin/alertas", "aria-current": rota === "#/admin/alertas" ? "page" : null },
          "Alertas", h("span", { class: "contador-menu", id: "menu-alertas-n", hidden: true })));
      atualizarContadorAlertas();
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
          h("p", { class: "nota" }, "Esqueceu a senha? Entre em contato com o administrador.")))));
}

function telaSenha() {
  const obrigatoria = eu.trocar_senha;
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const form = h("form", { class: "painel form-estreito" },
    h("h1", {}, obrigatoria ? "Defina a sua senha" : "Trocar senha"),
    obrigatoria ? h("p", { class: "nota" }, "Seu acesso foi feito com uma senha temporária. Crie uma senha pessoal para continuar.") : null,
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
            ? "Como administrador, você vê todos os sistemas. Para instalar ou configurar, acesse o painel de Soluções."
            : "Estes são os sistemas que você tem acesso.")),
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
      h("strong", {}, "Ainda sem acesso a nenhum sistema."),
      h("p", {}, "Solicite ao administrador do portal para liberar os sistemas que você precisa.")));
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
  if (sol.externo) {
    // Sobe por fora do portal (ex.: Supabase em deploy/supabase): só dá para abrir.
    if (sol.status.estado === "rodando") caixa.append(h("a", { class: "botao primario", href: sol.url, target: "_blank", rel: "noopener" }, "Abrir"));
    else if (!soPrincipal) caixa.append(h("span", { class: "nota" }, "Gerenciado pelo servidor: rode deploy/atualizar.sh na VM."));
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
          texto: `O container de ${sol.nome} será apagado. A imagem e os dados não são alterados; pode ser iniciado novamente quando necessário.`,
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
      h("div", {}, h("h1", {}, "Soluções"), h("p", {}, "Instale, inicie e gerencie cada sistema publicado no portal.")),
      h("div", { class: "intro-lado" },
        h("span", { id: "docker-slot" }),
        h("button", {
          type: "button", class: "botao",
          title: "Consulta novamente a pasta solucoes/ para incluir soluções novas ou alteradas",
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
        }, "Recarregar catálogo"),
        h("a", { class: "botao primario", href: "#/admin/nova-solucao" }, "Nova solução"))),
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
        h("h2", {}, h("a", { href: `#/admin/solucao/${sol.id}` }, sol.nome),
          sol.local ? h("span", { class: "etiqueta", title: "Criada pelo assistente localmente" }, "Neste servidor") : null),
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
     ["Banco", sol.banco ? `${sol.banco.nome} no PostgreSQL do servidor (usuário ${sol.banco.usuario})` : ""],
     ["Dockerfile", sol.dockerfile_portal ? "Gerado pelo assistente, guardado no portal" : ""],
     ["Origem", sol.local ? "Criada pelo assistente neste servidor (fora do Git do portal)" : ""]]
      .filter(([, v]) => v)
      .map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
  const blocoLocal = sol.local
    ? h("div", { class: "bloco" },
        h("h3", {}, "Migrar para outros servidores"),
        h("p", { class: "nota" }, `Esta solução só existe neste servidor. Para publicá-la em outro, baixe o .zip, descompacte na raiz do repositório do portal (cria solucoes/${sol.id}/), faça o commit e remova esta cópia, se necessário.`),
        h("div", { class: "linha-botoes topo-8" },
          h("a", { class: "botao pequeno", href: `#/admin/editar-solucao/${sol.id}` }, "Editar"),
          h("button", { type: "button", class: "botao pequeno", onclick: () => baixarArquivosLocais(sol.id) }, "Baixar arquivos (.zip)"),
          h("button", { type: "button", class: "botao pequeno perigo", onclick: () => removerLocal(sol) }, "Remover do catálogo")))
    : null;

  const blocoSupabase = sol.supabase ? blocoSupabaseDetalhe(sol) : null;

  const demo = sol.acessos_demo.length
    ? h("div", { class: "bloco" },
        h("h3", {}, "Acessos de demonstração/teste"),
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
      h("small", {}, `Deixe em branco para usar ${sol.url_padrao}. Com domínio próprio, informe por exemplo https://rh.dominio.com.br.`)),
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
      h("section", { class: "painel" }, h("h2", {}, "Ficha técnica"), ficha, blocoSupabase, blocoLocal)),
    config: h("div", { class: "aba-painel" },
      h("section", { class: "painel estreito" }, h("h2", {}, "Configurações"),
        h("p", { class: "nota acima" }, "Serão variáveis de ambiente da solução. As mudanças são aplicadas depois de Reiniciar."), formConfig)),
    acesso: h("div", { class: "aba-painel colunas" },
      h("section", { class: "painel" }, h("h2", {}, "Quem pode acessar"), pessoas),
      h("section", { class: "painel" }, h("h2", {}, "Endereço"), formEndereco)),
    logs: h("div", { class: "aba-painel" },
      h("section", { class: "painel logs" },
        h("div", { class: "logs-topo" },
          h("div", { class: "abas-mini", role: "tablist", "aria-label": "Tipo de log" },
            [["operacao", "Operação do portal"], ["aplicacao", "Saída da aplicação"]].map(([aba, rotulo]) =>
              h("button", { type: "button", role: "tab", "data-aba": aba, onclick: () => { abaLogs = aba; pintarLogs(true); } }, rotulo))),
          h("small", { class: "nota" }, "Atualiza automaticamente enquanto a página está aberta.")),
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

/** Bloco "Supabase do sistema" (visão geral): situação da pilha, variáveis e o Studio. */
function blocoSupabaseDetalhe(sol) {
  const sb = sol.supabase;
  const est = sb.estado;
  const partes = est ? h("ul", { class: "lista-partes" }, est.partes.map((p) =>
    h("li", { "data-situacao": p.situacao === "no ar" ? "ok" : p.situacao === "nao criado" ? "vazio" : "alerta" },
      h("span", {}, p.rotulo), h("small", {}, p.situacao === "nao criado" ? "não criado" : p.situacao)))) : null;
  const botaoStudio = sb.studio && est?.no_ar ? h("button", {
    type: "button", class: "botao pequeno",
    onclick: async (ev) => {
      const b = ev.currentTarget;
      // Abre a aba já no clique (senão o navegador bloqueia) e manda para o Studio quando ele responder.
      const janela = window.open("about:blank", "_blank");
      b.disabled = true;
      const texto = b.textContent;
      b.textContent = "Abrindo o Studio…";
      try {
        const r = await api(`/api/solucoes/${sol.id}/supabase/studio`, { method: "POST" });
        if (janela) janela.location = r.url; else window.open(r.url, "_blank", "noopener");
      } catch (e) { janela?.close(); toast(e.message, "erro"); }
      b.disabled = false;
      b.textContent = texto;
    },
  }, est.studio_neste ? "Abrir o Studio" : "Abrir o Studio desta solução") : null;
  return h("div", { class: "bloco bloco-supabase" },
    h("h3", {}, "Supabase da solução"),
    h("p", { class: "nota" }, "Banco, login e arquivos desta aplicação ficam no mesmo endereço (/rest/v1, /auth/v1 e /storage/v1), "
      + "atrás do login do portal. O portal preenche automaticamente:"),
    h("ul", { class: "lista-variaveis" }, sb.variaveis.map((v) => h("li", {}, h("code", {}, v.nome), h("span", {}, v.conteudo)))),
    est && !est.criado ? h("p", { class: "nota" }, "Ainda não criado: é criado na primeira inicialização (baixa as imagens e isso pode levar alguns minutos).") : partes,
    est?.criado && !est.no_ar ? h("p", { class: "nota" }, "Fora do ar. Tente Reiniciar a solução.") : null,
    h("p", { class: "nota" }, "Os dados ficam no servidor. Reiniciar, atualizar ou remover o container não os apaga."),
    est?.no_ar ? h("div", { class: "linha-botoes topo-8" }, botaoStudio,
      h("button", { type: "button", class: "botao pequeno", onclick: () => dialogoCopiarSupabase(sol) }, "Importar dados de outra base (Supabase)"),
      h("button", { type: "button", class: "botao pequeno", onclick: () => dialogoEstruturaSupabase(sol) }, "Criar a estrutura pelos arquivos .sql"),
      sb.importacao?.existe ? h("button", { type: "button", class: "botao pequeno perigo", onclick: () => dialogoDesfazerImportacao(sol) },
        "Desfazer a última importação") : null) : null,
    sb.importacao?.existe ? h("p", { class: "nota" }, `Última importação: ${dataIso(sb.importacao.em.replace(" ", "T"))}, de ${sb.importacao.origem}.`) : null,
    sb.studio && !est?.no_ar ? h("p", { class: "nota" }, "O Studio (painel de administração do banco) abre quando o Supabase estiver no ar.") : null,
    blocoLoginSupabase(sol));
}

/** Projeto sem Supabase de origem: cria as tabelas pelos .sql do repositório (banco vazio). */
async function dialogoEstruturaSupabase(sol) {
  const corpo = h("div", {}, h("p", { class: "nota" }, h("span", { class: "girando", "aria-hidden": "true" }), " Procurando os arquivos .sql…"));
  const rodape = h("div", { class: "linha-botoes" });
  const fechar = h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Fechar");
  rodape.replaceChildren(fechar);
  const dlg = dialogo({ titulo: "Criar a estrutura pelos arquivos .sql", descricao: sol.nome, corpo, rodape, largo: true });
  dlg.addEventListener("close", () => telaSolucao(sol.id), { once: true });
  let d;
  try { d = await api(`/api/solucoes/${sol.id}/supabase/estrutura`); } catch (e) {
    corpo.replaceChildren(h("p", { class: "form-erro" }, e.message)); return;
  }
  if (d.execucao?.estado === "executando") { acompanhar(); return; }
  if (d.tabelas > 0) {
    corpo.replaceChildren(h("p", { class: "dialogo-texto" }, `O banco deste sistema já tem ${plural(d.tabelas, "tabela", "tabelas")}. `
      + "A criação pelos arquivos .sql só roda num banco vazio. Para trazer dados de outro Supabase, use \"Importar dados de outra base\"."),
      d.execucao ? h("details", { class: "avancado" }, h("summary", {}, "Última execução"), h("pre", { class: "saida curta" }, d.execucao.log)) : null);
    return;
  }
  if (!d.arquivos.length) {
    corpo.replaceChildren(h("p", { class: "dialogo-texto" }, "Nenhum arquivo .sql encontrado nas pastas supabase/migrations, migrations, sql, database, db ou supabase do repositório."));
    return;
  }
  const marcas = d.arquivos.map((a) => h("label", { class: "item-sql" },
    h("input", { type: "checkbox", value: a.caminho, checked: true }), h("code", {}, a.caminho), h("small", {}, tamanho(a.bytes))));
  const conf = h("input", { type: "text", id: "es-conf", autocomplete: "off", spellcheck: false });
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const contagem = h("strong", {});
  const atualizar = () => {
    const n = marcas.filter((m) => m.querySelector("input").checked).length;
    contagem.textContent = plural(n, "arquivo marcado", "arquivos marcados");
    botao.disabled = !n || conf.value.trim() !== sol.id;
  };
  const botao = h("button", { type: "button", class: "botao primario", disabled: true, onclick: async () => {
    botao.disabled = true;
    try {
      await api(`/api/solucoes/${sol.id}/supabase/estrutura`, { method: "POST", body: {
        arquivos: marcas.map((m) => m.querySelector("input")).filter((i) => i.checked).map((i) => i.value), confirmacao: conf.value.trim() } });
      acompanhar();
    } catch (e) { erro.textContent = e.message; erro.hidden = false; botao.disabled = false; }
  } }, "Criar a estrutura");
  for (const m of marcas) m.querySelector("input").addEventListener("change", atualizar);
  conf.addEventListener("input", atualizar);
  corpo.replaceChildren(
    h("p", { class: "dialogo-texto" }, "O banco está vazio. Os arquivos marcados rodam nesta ordem, cada um numa transação. No primeiro erro, para e mostra o arquivo; os anteriores ficam aplicados. Antes, o portal faz um backup do Supabase deste sistema."),
    h("p", { class: "nota" }, "Confira a ordem: a sugerida é schema* primeiro, depois os outros pela numeração, seed* por último. Desmarque o que não deve rodar (por exemplo, migrações que o schema.sql já inclui). Código da última instalação ou atualização."),
    h("div", { class: "lista-sql" }, marcas),
    h("p", { class: "nota" }, contagem),
    h("div", { class: "campo" }, h("label", { for: "es-conf" }, `Para confirmar, digite ${sol.id}`), conf),
    erro);
  rodape.replaceChildren(fechar, botao);
  atualizar();

  function acompanhar() {
    const etapa = h("p", { class: "etapa-copia" }, "Iniciando…");
    const pre = h("pre", { class: "saida curta", tabindex: "0" }, "");
    const fim = h("div", {});
    corpo.replaceChildren(etapa, pre, fim);
    rodape.replaceChildren(fechar);
    const passo = async () => {
      if (!dlg.isConnected) return;
      let c;
      try { c = (await api(`/api/solucoes/${sol.id}/supabase/estrutura/situacao`)).execucao; } catch { setTimeout(passo, 3000); return; }
      if (!c) return;
      pre.textContent = c.log;
      pre.scrollTop = pre.scrollHeight;
      if (c.estado === "executando") {
        etapa.replaceChildren(h("span", { class: "girando", "aria-hidden": "true" }), ` ${c.etapa || "Trabalhando nisso"}… Essa janela pode ser fechada, continua mesmo assim.`);
        setTimeout(passo, 2000);
        return;
      }
      etapa.replaceChildren(pilula(c.estado === "falhou" ? "com_problema" : "rodando", c.estado === "falhou" ? "Falhou" : "Concluída"),
        c.estado !== "falhou" ? ` ${plural(c.resumo.aplicados, "arquivo aplicado", "arquivos aplicados")}, ${plural(c.resumo.tabelas, "tabela", "tabelas")}.` : "");
      fim.replaceChildren(c.erro ? h("p", { class: "form-erro" }, c.erro) : null,
        c.resumo?.seguranca ? h("p", { class: "nota" }, `Backup de antes: ${c.resumo.seguranca}. Para voltar a ele, use "Desfazer a última importação".`) : null);
    };
    passo();
  }
}

/** Volta o Supabase ao backup feito antes da última importação. */
function dialogoDesfazerImportacao(sol) {
  const imp = sol.supabase.importacao;
  const conf = h("input", { type: "text", id: "dz-conf", autocomplete: "off", spellcheck: false });
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const botao = h("button", { type: "button", class: "botao perigo-cheio", disabled: true, onclick: async () => {
    botao.disabled = true;
    try {
      await api(`/api/solucoes/${sol.id}/supabase/desfazer`, { method: "POST", body: { confirmacao: conf.value.trim() } });
      dlg.close();
      toast("Desfazendo a importação. Acompanhe em Backups.");
      location.hash = "#/admin/backups";
    } catch (e) { erro.textContent = e.message; erro.hidden = false; botao.disabled = false; }
  } }, "Desfazer a importação");
  conf.addEventListener("input", () => { botao.disabled = conf.value.trim() !== sol.id; });
  const dlg = dialogo({
    titulo: "Desfazer a última importação?",
    descricao: sol.nome,
    corpo: [
      h("p", { class: "dialogo-texto" }, `O Supabase deste sistema volta ao estado de antes da última importação (${dataIso(imp.em.replace(" ", "T"))}, de ${imp.origem}): banco, usuários do login, arquivos e chaves.`),
      h("p", { class: "dialogo-texto" }, "Tudo o que foi gravado no sistema depois da importação se perde. Antes, o portal faz outro backup de segurança (dá para voltar a ele em Backups)."),
      h("div", { class: "campo" }, h("label", { for: "dz-conf" }, `Para confirmar, digite ${sol.id}`), conf),
      erro,
    ],
    rodape: [h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"), botao],
  });
}

/** E-mail do login (convite, confirmação, esqueci a senha) e cadastro aberto ou fechado. */
function blocoLoginSupabase(sol) {
  const lg = sol.supabase.login;
  if (!lg) return null;
  const origem = { sistema: "e-mail próprio deste sistema", servidor: `e-mail do servidor (${lg.servidor_global})`,
    nenhum: "nenhum: sem e-mail, as contas novas são confirmadas automaticamente e \"esqueci a senha\" não envia nada" }[lg.origem_smtp];
  const resumo = h("p", { class: "nota" }, `Envio de e-mail: ${origem}. Cadastro ${lg.cadastro_aberto ? "aberto" : "fechado"}.`);
  return h("details", { class: "avancado topo-8" }, h("summary", {}, "Login e e-mail"), resumo,
    h("button", { type: "button", class: "botao pequeno", onclick: () => dialogoLoginSupabase(sol) }, "Configurar login e e-mail"));
}

function dialogoLoginSupabase(sol) {
  const lg = sol.supabase.login;
  const campo = (id, rotulo, valor, extra = {}, ajuda = "") => h("div", { class: "campo" },
    h("label", { for: id }, rotulo), h("input", { id, type: "text", value: valor ?? "", autocomplete: "off", spellcheck: false, ...extra }),
    ajuda ? h("small", {}, ajuda) : null);
  const aberto = h("input", { type: "checkbox", id: "lg-cadastro", checked: lg.cadastro_aberto });
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const ok = h("p", { class: "nota", role: "status", hidden: true });
  const form = h("form", { id: "form-login-sb", class: "config" },
    h("p", { class: "nota acima" }, "E-mail usado pelo login do sistema para convites, confirmação de conta e \"esqueci a senha\". "
      + (lg.servidor_global ? `Em branco, usa o do servidor (${lg.servidor_global}).` : "Em branco, o sistema não envia e-mails.")),
    h("div", { class: "form-grade" },
      campo("lg-servidor", "Servidor de e-mail (SMTP)", lg.smtp_servidor, { placeholder: "smtp.office365.com" }),
      campo("lg-porta", "Porta", lg.smtp_porta || "587", { type: "number" }, "587 (STARTTLS) ou 465 (SSL)."),
      campo("lg-usuario", "Usuário", lg.smtp_usuario)),
    h("div", { class: "form-grade" },
      campo("lg-senha", "Senha", "", { type: "password", autocomplete: "new-password", placeholder: lg.tem_senha ? "•••••••• (guardada)" : "" },
        lg.tem_senha ? "Em branco mantém a senha guardada." : ""),
      campo("lg-remetente", "E-mail do remetente", lg.smtp_remetente, { inputmode: "email", placeholder: "nao-responda@log20.com.br" }),
      campo("lg-nome", "Nome do remetente", lg.smtp_nome, { placeholder: "Portal LOG20" })),
    h("label", { class: "campo-check" }, aberto,
      h("span", {}, h("strong", {}, "Cadastro aberto"),
        h("small", {}, " Permite criar contas pelo próprio sistema. O Panorama cria os usuários dele assim (tela de Administração): "
          + "fechado, essa tela deixa de funcionar. Só quem entrou no portal e tem acesso ao sistema chega ao login dele."))),
    erro, ok);
  const valores = () => ({ smtp_servidor: form.querySelector("#lg-servidor").value.trim(), smtp_porta: form.querySelector("#lg-porta").value.trim(),
    smtp_usuario: form.querySelector("#lg-usuario").value.trim(), smtp_senha: form.querySelector("#lg-senha").value,
    smtp_remetente: form.querySelector("#lg-remetente").value.trim(), smtp_nome: form.querySelector("#lg-nome").value.trim(),
    cadastro_aberto: aberto.checked });
  const testar = h("button", { type: "button", class: "botao", onclick: async () => {
    testar.disabled = true; erro.hidden = true; ok.hidden = true;
    try {
      const r = await api(`/api/solucoes/${sol.id}/supabase/login/testar`, { method: "POST", body: { dados: valores() } });
      ok.textContent = r.mensagem; ok.hidden = false;
    } catch (e) { erro.textContent = e.message; erro.hidden = false; }
    testar.disabled = false;
  } }, "Testar conexão");
  const salvar = h("button", { type: "submit", class: "botao primario", form: "form-login-sb" }, "Salvar");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    salvar.disabled = true; erro.hidden = true; ok.hidden = true;
    try {
      const r = await api(`/api/solucoes/${sol.id}/supabase/login`, { method: "PUT", body: { dados: valores() } });
      dlg.close();
      toast(r.aplicado ? "Login e e-mail salvos e aplicados." : "Salvo. Vale a partir do próximo início do sistema.");
      telaSolucao(sol.id);
    } catch (e) { erro.textContent = e.message; erro.hidden = false; salvar.disabled = false; }
  });
  const dlg = dialogo({ titulo: "Login e e-mail", descricao: sol.nome, corpo: form, largo: true,
    rodape: [h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"), testar, salvar] });
}

/** Copiar tabelas, usuários, regras e arquivos de outro Supabase (ex.: o da nuvem). */
async function dialogoCopiarSupabase(sol) {
  let atual = null;
  try { atual = (await api(`/api/solucoes/${sol.id}/supabase/copiar`)).copia; } catch { /* sem cópia anterior */ }
  const corpo = h("div", {});
  const rodape = h("div", { class: "linha-botoes" });
  const dlg = dialogo({ titulo: "Importar dados de outra base (Supabase)", descricao: sol.nome, corpo, rodape, largo: true });
  if (atual?.estado === "executando") { acompanhar(); return; }

  const campo = (id, rotulo, extra = {}, ajuda = "") => h("div", { class: "campo" },
    h("label", { for: id }, rotulo), h("input", { id, autocomplete: "off", spellcheck: false, ...extra }), ajuda ? h("small", {}, ajuda) : null);
  const marca = (nome, rotulo, ajuda, marcado = true) => h("label", { class: "campo-check" },
    h("input", { type: "checkbox", name: nome, checked: marcado }), h("span", {}, h("strong", {}, rotulo), h("small", {}, ` ${ajuda}`)));
  const blocoArquivos = h("div", { class: "form-grade" },
    campo("cp-url", "Endereço do projeto de origem", { type: "text", placeholder: "https://<id>.supabase.co" }, "Em branco: obtido do usuário (postgres.<id>)."),
    campo("cp-chave", "Chave de serviço da origem", { type: "password" }, "Project Settings → API Keys: service_role (ou secret)."));
  const form = h("form", { id: "form-copiar", class: "config" },
    h("p", { class: "nota acima" }, "O que for importado irá substituir o que estiver no banco desta solução. Antes, o portal faz um backup do Supabase deste sistema (banco, usuários, arquivos e chaves): se algo der errado, use \"Desfazer a última importação\"."),
    h("div", { class: "form-grade" },
      campo("cp-host", "Servidor", { type: "text", required: true, placeholder: "aws-1-sa-east-1.pooler.supabase.com" }),
      campo("cp-porta", "Porta", { type: "number", value: "5432", required: true }),
      campo("cp-usuario", "Usuário", { type: "text", required: true, placeholder: "postgres.<id do projeto>" })),
    h("div", { class: "form-grade" },
      campo("cp-senha", "Senha do banco de origem", { type: "password", required: true, autocomplete: "new-password" },
        "Usada apenas nesta cópia.")),
    h("small", { class: "sem-topo" }, "No painel da Supabase: Connect → Session pooler. A senha é a do banco: Project Settings → Database."),
    h("div", { class: "escolhas-copia" },
      marca("tabelas", "Tabelas e dados", "Esquema public inteiro: tabelas, dados, funções, gatilhos e regras (RLS)."),
      marca("usuarios", "Usuários do login", "Com as mesmas senhas. Devem ser copiadas junto com as tabelas."),
      marca("storage", "Buckets e regras dos arquivos", "Os buckets e quem pode ler e salvar em cada um."),
      marca("arquivos", "Arquivos", "Baixa cada arquivo da origem. Precisa da chave de serviço da origem.")),
    blocoArquivos,
    campo("cp-confirma", `Para confirmar, digite ${sol.id}`, { type: "text", required: true }));
  form.querySelector("[name=arquivos]").addEventListener("change", (ev) => {
    blocoArquivos.hidden = !ev.target.checked;
    if (ev.target.checked) form.querySelector("[name=storage]").checked = true;  // arquivo só entra num bucket que existe
  });
  const botao = h("button", { type: "submit", class: "botao perigo-cheio", form: "form-copiar" }, "Copiar e substituir");
  corpo.replaceChildren(form);
  rodape.replaceChildren(h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"), botao);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const v = (id) => form.querySelector(`#${id}`).value.trim();
    const partes = Object.fromEntries(["tabelas", "usuarios", "storage", "arquivos"].map((k) => [k, form.querySelector(`[name=${k}]`).checked]));
    botao.disabled = true;
    try {
      await api(`/api/solucoes/${sol.id}/supabase/copiar`, { method: "POST", body: { dados: {
        host: v("cp-host"), porta: Number(v("cp-porta")), usuario: v("cp-usuario"), senha: form.querySelector("#cp-senha").value,
        url: v("cp-url"), chave_servico: form.querySelector("#cp-chave").value, partes, confirmacao: v("cp-confirma") } } });
      acompanhar();
    } catch (e) { toast(e.message, "erro"); botao.disabled = false; }
  });

  function acompanhar() {
    const etapa = h("p", { class: "etapa-copia" }, "Iniciando…");
    const pre = h("pre", { class: "saida curta", tabindex: "0" }, "");
    const fim = h("div", {});
    corpo.replaceChildren(etapa, pre, fim);
    rodape.replaceChildren(h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Fechar"));
    const passo = async () => {
      if (!dlg.isConnected) return;
      let c;
      try { c = (await api(`/api/solucoes/${sol.id}/supabase/copiar`)).copia; } catch { setTimeout(passo, 3000); return; }
      if (!c) return;
      pre.textContent = c.log;
      pre.scrollTop = pre.scrollHeight;
      if (c.estado === "executando") {
        etapa.replaceChildren(h("span", { class: "girando", "aria-hidden": "true" }), ` ${c.etapa || "Trabalhando nisso"}… Essa janela pode ser fechada, a cópia continua mesmo assim.`);
        setTimeout(passo, 2000);
        return;
      }
      const r = c.resumo || {};
      const itens = [r.tabelas != null && `${r.tabelas} tabela(s)`, r.usuarios != null && `${r.usuarios} usuário(s)`,
        r.buckets != null && `${r.buckets} bucket(s)`, r.arquivos != null && `${r.arquivos} arquivo(s)`].filter(Boolean);
      etapa.replaceChildren(pilula(c.estado === "falhou" ? "com_problema" : "rodando",
        c.estado === "falhou" ? "Falhou" : c.estado === "concluida" ? "Concluída" : "Concluída com avisos"),
        itens.length && c.estado !== "falhou" ? ` Copiado: ${itens.join(", ")}.` : "");
      fim.replaceChildren(
        c.erro ? h("p", { class: "form-erro" }, c.erro) : null,
        c.avisos?.length ? h("details", { class: "avancado" }, h("summary", {}, `Avisos (${c.avisos.length})`),
          h("ul", { class: "lista-explica" }, c.avisos.map((a) => h("li", {}, a)))) : null,
        c.estado !== "falhou" ? h("p", { class: "nota" }, "Abra o sistema e entre com um usuário válido. Se algum problema ocorrer, clique em Reiniciar na solução.") : null,
        c.resumo?.seguranca ? h("p", { class: "nota" }, `Backup de antes da importação: ${c.resumo.seguranca}. Para voltar a ele, feche esta janela e use "Desfazer a última importação".`) : null);
      if (dlg.isConnected) dlg.addEventListener("close", () => telaSolucao(sol.id), { once: true });
    };
    passo();
  }
}

function mostrarAba(k) {
  abaSolucao = k;
  for (const b of $conteudo.querySelectorAll(".abas > button")) b.setAttribute("aria-selected", String(b.id === `aba-${k}`));
  for (const p of $conteudo.querySelectorAll(".aba-painel")) p.hidden = p.id !== `painel-${k}`;
  if (k === "logs") pintarLogs(true);
}

async function carregarPessoas(sol) {
  const alvo = document.getElementById("pessoas-solucao");
  const avisoAberto = sol.aberto ? h("p", { class: "nota" }, "Acesso direto: o endereço deste sistema abre sem o login do portal, com o login do próprio sistema. Aqui você escolhe só quem o vê em Meus sistemas.") : null;
  if (sol.somente_admin) {
    alvo.replaceChildren(h("p", { class: "numero-grande" }, "Só", h("span", {}, "administradores")),
      h("p", { class: "nota" }, "Este sistema dá acesso total a bancos de dados: só os administradores do portal abrem. Não pode ser liberado em Acessos."));
    return;
  }
  try {
    const { usuarios } = await api("/api/usuarios");
    const comAcesso = usuarios.filter((u) => u.ativo && u.perfil !== "admin" && u.solucoes.includes(sol.id));
    const admins = usuarios.filter((u) => u.ativo && u.perfil === "admin").length;
    alvo.replaceChildren(
      avisoAberto,
      h("p", { class: "numero-grande" }, comAcesso.length, h("span", {}, comAcesso.length === 1 ? "pessoa com acesso" : "pessoas com acesso")),
      h("p", { class: "nota" }, `Além de ${plural(admins, "administrador", "administradores")}, que acessam todos os sistemas.`),
      comAcesso.length
        ? h("ul", { class: "pessoas" }, comAcesso.slice(0, 12).map((u) => h("li", {}, avatar(u, "pequeno"), u.nome)),
            comAcesso.length > 12 ? h("li", { class: "nota" }, `e mais ${comAcesso.length - 12}`) : null)
        : null,
      h("a", { class: "botao", href: "#/admin/acessos", onclick: () => { estadoAcessos.aba = "sistema"; estadoAcessos.sistema = sol.id; } }, "Gerenciar acessos"));
  } catch (e) { alvo.replaceChildren(h("p", { class: "nota" }, e.message)); }
}

function montarFormulario(form, sol) {
  form.replaceChildren();
  if (!sol.configuracoes.length) {
    form.append(h("p", { class: "nota" }, "Esta solução não tem configurações."));
    return;
  }
  if (sol.configuracoes.some((c) => c.construcao)) {
    form.append(h("p", { class: "nota acima" }, "Campos “Na construção” são gravados no sistema quando ele é criado (ex.: sites Vite ou React): depois de mudar, é preciso Atualizar, não apenas Reiniciar para aplicar as alterações."));
  }
  for (const c of sol.configuracoes) {
    const idCampo = `cfg-${c.nome}`;
    if (c.gerenciada) {
      form.append(h("div", { class: "campo" },
        h("label", { for: idCampo }, c.rotulo, h("span", { class: "etiqueta" }, "Preenchido automaticamente")),
        h("input", { id: idCampo, type: "text", disabled: true, value: "", placeholder: "Obtido do Supabase do sistema (Visão geral)" }),
        h("small", {}, "Este valor é preenchido automaticamente com o Supabase próprio da solução.")));
      continue;
    }
    const limpar = c.tipo === "segredo" && c.definido
      ? h("button", {
          type: "button", class: "link-limpar",
          onclick: async () => {
            if (!(await confirmar({ titulo: "Apagar o valor salvo?", texto: `${c.rotulo}: o valor salvo no portal será apagado.`, botao: "Apagar valor", perigo: true }))) return;
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
          c.obrigatoria && !c.definido ? h("span", { class: "etiqueta obrigatoria" }, "Obrigatório") : null,
          c.construcao ? h("span", { class: "etiqueta", title: "Gravado no sistema no momento da criação. A alteração só é aplicada depois de Atualizar" }, "Na criação") : null),
        h("input", {
          id: idCampo, name: c.nome, autocomplete: "off",
          type: c.tipo === "segredo" ? "password" : "text",
          value: c.tipo === "segredo" ? "" : (c.valor ?? ""),
          placeholder: c.tipo === "segredo" ? (c.definido ? "Deixe em branco para manter o valor salvo" : "Não definido") : null,
        }), ajuda));
    }
  }
  const restaurarBanco = sol.campos_banco?.length ? h("button", {
    type: "button", class: "botao sutil",
    onclick: async () => {
      if (!(await confirmar({ titulo: "Usar o banco do servidor?", texto: `Os campos ${sol.campos_banco.join(", ")} voltam a apontar para o banco desta solução no PostgreSQL do servidor. Os dados do banco não são alterados.`, botao: "Usar o banco do servidor" }))) return;
      try {
        const r = await api(`/api/solucoes/${sol.id}/banco/restaurar`, { method: "POST" });
        montarFormulario(form, await api(`/api/solucoes/${sol.id}`));
        toast(r.aviso);
      } catch (e) { toast(e.message, "erro"); }
    },
  }, "Usar o banco do servidor") : null;
  form.append(h("div", { class: "linha-botoes" }, h("button", { type: "submit", class: "botao primario" }, "Salvar configurações"), restaurarBanco));
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    // Envia só o que foi mudado nesta tela: assim um formulário aberto antes de uma operação
    // (ex.: o portal preencheu o banco ao iniciar) não apaga os valores gravados depois.
    const valores = {};
    for (const c of sol.configuracoes) {
      if (c.gerenciada) continue;
      const input = form.elements[c.nome];
      if (c.tipo === "booleano") { if (input.checked !== (c.valor === "true")) valores[c.nome] = input.checked; }
      else if (c.tipo === "segredo") { if (input.value) valores[c.nome] = input.value; }
      else if (input.value !== (c.valor ?? "")) valores[c.nome] = input.value;
    }
    if (!Object.keys(valores).length) { toast("Nada mudou."); return; }
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
      h("div", {}, h("h1", {}, "Usuários"), h("p", {}, "Quem pode entrar no portal. Para escolher quem vê cada sistema, use o painel de Acessos.")),
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
      dados.usuarios.length ? "Nenhum usuário encontrado com esse filtro." : "Nenhum usuário cadastrado.")));
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
      algumInativo ? h("button", { type: "button", class: "botao pequeno perigo", onclick: () => removerUsuarios(sel) }, "Remover desativados") : null,
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
      + (comuns.length < sel.length ? ` Administradores não são afetados. Eles já veem todos os sistemas.` : ""),
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
            toast(r.alterados ? `${conceder ? "Acesso liberado" : "Acesso retirado"}: ${plural(r.alterados, "permissão alterada", "permissões alteradas")}.` : "Nenhuma configuração alterada.");
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
    texto: "Essas pessoas são desconectadas do portal na hora e não conseguem entrar de novo até serem reativadas. Os acessos liberados ficam guardados.",
    botao: "Desativar", perigo: true,
  }))) return;
  try {
    const r = await api("/api/usuarios/situacao", { method: "POST", body: { usuarios: alvos.map((u) => u.id), ativo } });
    if (r.erros.length) toast(`${r.alterados} alterado(s). Não foi possível: ${r.erros.join("; ")}`, "erro");
    else toast(`${plural(r.alterados, "usuário", "usuários")} ${ativo ? "reativado(s)" : "desativado(s)"}.`);
    await carregarUsuarios(); pintarUsuarios();
  } catch (e) { toast(e.message, "erro"); }
}

/** Remove de vez os desativados da seleção (os ativos ficam: é preciso desativar antes). */
async function removerUsuarios(sel) {
  const alvos = sel.filter((u) => !u.ativo);
  const ativos = sel.length - alvos.length;
  if (!alvos.length) return;
  const nomesLista = alvos.slice(0, 8).map((u) => u.nome).join(", ") + (alvos.length > 8 ? ` e mais ${alvos.length - 8}` : "");
  if (!(await confirmar({
    titulo: `Remover ${plural(alvos.length, "usuário desativado", "usuários desativados")}?`,
    texto: `${nomesLista}. O cadastro e os acessos liberados são apagados de vez e não dá para desfazer. `
      + "O histórico de alterações continua mostrando o que essas pessoas fizeram, e o login fica livre para ser cadastrado de novo."
      + (ativos ? ` ${plural(ativos, "usuário ativo selecionado não é removido", "usuários ativos selecionados não são removidos")}: desative antes.` : ""),
    botao: `Remover ${plural(alvos.length, "usuário", "usuários")}`, perigo: true,
  }))) return;
  try {
    const r = await api("/api/usuarios/remover", { method: "POST", body: { usuarios: alvos.map((u) => u.id) } });
    for (const u of alvos) estadoUsuarios.selecionados.delete(u.id);
    if (r.erros.length) toast(`${plural(r.removidos, "removido", "removidos")}. Não foi possível: ${r.erros.join("; ")}`, "erro");
    else toast(`${plural(r.removidos, "usuário removido", "usuários removidos")}.`);
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
        h("small", {}, novo ? "Ex.: maria.silva ou a matrícula. Não é alterado depois." : "O login não pode ser alterado."))),
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
      if (!(await confirmar({ titulo: `Redefinir a senha de ${u.nome}?`, texto: "A senha atual deixa de ser válida e o usuário é desconectado do portal. Uma senha temporária é gerada para que o usuário possa acessar.", botao: "Redefinir senha" }))) return;
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
    descricao: novo ? "O usuário recebe uma senha temporária e cria a própria senha no primeiro acesso." : `Criado em ${quando(u.criado_em, true)} · último acesso: ${quando(u.ultimo_login)}`,
    corpo: form,
    rodape: [
      redefinir,
      !novo && !u.ativo ? h("button", { type: "button", class: "botao sutil perigo-texto",
        onclick: () => { dlg.close(); removerUsuarios([u]); } }, "Remover") : null,
      h("span", { class: "espaco" }),
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
      : comErro.length ? h("span", { class: "texto-erro" }, `${plural(comErro.length, "linha precisa", "linhas precisam")} de ajuste. Corrija no texto acima. Só é possível criar quando os erros forem corrigidos.`)
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
        h("small", { id: "lote-ajuda" }, "Uma pessoa por linha: login, nome, perfil. O perfil é opcional (padrão: usuário). "
          + "É possível colar direto do Excel, com as colunas nessa ordem.")),
      h("div", { class: "linha-botoes" },
        h("label", { for: "lote-arquivo", class: "botao pequeno" }, "Abrir arquivo CSV"), arquivo,
        h("button", { type: "button", class: "botao pequeno sutil", onclick: () => baixarCsv("modelo-usuarios.csv", [["login", "nome", "perfil"], ["maria.silva", "Maria Silva", "usuario"], ["10452", "Ana Paula Lima", "usuario"]]) }, "Baixar modelo"))),
    h("fieldset", { class: "grupo-campos" }, h("legend", {}, "Liberar para todos os importados"), escolha.el,
      h("p", { class: "nota" }, "Opcional. Administradores da lista já veem tudo.")),
    h("div", {}, resumo, previa),
    erroGeral);

  const dlg = dialogo({
    titulo: "Importar usuários em lote",
    descricao: "Cada pessoa recebe uma senha temporária. O download da lista com as senhas é feito no final.",
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
    descricao: nomesSol.length ? `Com acesso a ${nomesSol.join(", ")}.` : "Sem sistemas liberados ainda. Use o painel de Acessos para liberar.",
    largo: true,
    corpo: [
      h("div", { class: "senha-temp" },
        h("strong", {}, "Salve as senhas agora"),
        h("small", {}, "Elas não serão mostradas de novo. Entregue cada senha somente à pessoa dona do login e apague o arquivo depois. No primeiro acesso cada um cria a própria senha.")),
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
          if (!baixou && !(await confirmar({ titulo: "Fechar sem baixar as senhas?", texto: "Depois de fechar, só é possível gerar senhas novas com Redefinir senha, uma pessoa por vez.", botao: "Fechar mesmo assim" }))) return;
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

const estadoAcessos = { aba: "sistema", buscaHist: "", sistema: null, pessoa: null, buscaSis: "", buscaPes: "", inativos: false, filtroQuem: "", todos: false };

async function telaAcessos() {
  const abas = [["sistema", "Por sistema"], ["pessoa", "Por pessoa"], ["historico", "Histórico de alterações"]];
  if (!abas.some(([k]) => k === estadoAcessos.aba)) estadoAcessos.aba = "sistema";
  $conteudo.replaceChildren(
    h("section", { class: "intro" },
      h("h1", {}, "Acessos"),
      h("p", {}, "Quem pode abrir cada sistema. A mudança vale na hora.")),
    h("div", { class: "abas", role: "tablist" }, abas.map(([k, rotulo]) =>
      h("button", { type: "button", role: "tab", "aria-selected": String(estadoAcessos.aba === k), onclick: () => { estadoAcessos.aba = k; telaAcessos(); } }, rotulo))),
    h("div", { id: "acessos-corpo" }, h("p", { class: "nota" }, "Carregando…")));
  if (estadoAcessos.aba === "historico") { await pintarHistorico(); return; }
  await carregarUsuarios();
  if (estadoAcessos.aba === "pessoa") pintarPorPessoa(); else pintarPorSistema();
}

/* ---- dados e ações comuns das abas de acesso ---- */

const ehUsuarioComum = (u) => u.perfil !== "admin";
const ativosComuns = () => estadoUsuarios.dados.usuarios.filter((u) => u.ativo && ehUsuarioComum(u));
const adminsAtivos = () => estadoUsuarios.dados.usuarios.filter((u) => u.ativo && !ehUsuarioComum(u));
const comAcesso = (sid) => estadoUsuarios.dados.usuarios.filter((u) => ehUsuarioComum(u) && u.solucoes.includes(sid));

/** Sistemas agrupados por área; os "só administradores" no fim de cada área e áreas só deles por último. */
function sistemasPorArea(filtro = "") {
  const termo = normalizar(filtro);
  const lista = estadoUsuarios.dados.solucoes.filter((s) => !termo || normalizar(`${s.nome} ${s.categoria}`).includes(termo));
  const areas = new Map();
  for (const s of [...lista].sort((a, b) => (a.somente_admin === true) - (b.somente_admin === true) || a.nome.localeCompare(b.nome, "pt-BR"))) {
    const a = s.categoria || "Geral";
    if (!areas.has(a)) areas.set(a, []);
    areas.get(a).push(s);
  }
  return [...areas.entries()].sort(([a, la], [b, lb]) =>
    (la.every((s) => s.somente_admin) - lb.every((s) => s.somente_admin)) || a.localeCompare(b, "pt-BR"));
}

/** Libera ou retira e atualiza os dados da tela (sem recarregar tudo). */
async function mudarAcesso(uids, sids, conceder) {
  if (!uids.length || !sids.length) return 0;
  const r = await api("/api/acessos", { method: "POST", body: { usuarios: uids, solucoes: sids, conceder } });
  for (const u of estadoUsuarios.dados.usuarios) {
    if (!uids.includes(u.id) || !ehUsuarioComum(u)) continue;
    const atual = new Set(u.solucoes);
    for (const s of sids) { if (conceder) atual.add(s); else atual.delete(s); }
    u.solucoes = [...atual].sort();
  }
  return r.alterados;
}

function repintarAcessos() {
  if (estadoAcessos.aba === "pessoa") pintarPorPessoa(); else pintarPorSistema();
}

function itemLado(ativo, conteudo, onclick) {
  return h("button", { type: "button", class: `item-lado${ativo ? " ativo" : ""}`, "aria-current": ativo ? "true" : null,
    onclick: () => { onclick(); rolarParaDetalhe(); } }, conteudo);
}

/** No celular o detalhe fica embaixo da lista: leva a tela até ele. */
function rolarParaDetalhe() {
  if (window.matchMedia("(max-width: 900px)").matches) {
    document.querySelector(".detalhe-acesso")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

/** Mantém o item escolhido visível na lista da esquerda depois de repintar. */
function manterRolagem(lista, antes) {
  if (antes != null) lista.scrollTop = antes;
}

/* ---- aba "Por sistema" ---- */

function pintarPorSistema() {
  const alvo = document.getElementById("acessos-corpo");
  if (!alvo) return;
  const { solucoes } = estadoUsuarios.dados;
  if (!solucoes.length) {
    alvo.replaceChildren(h("div", { class: "vazio" }, h("strong", {}, "Nenhuma solução no catálogo"), h("p", {}, "Publique uma solução em Soluções para liberar acesso a ela.")));
    return;
  }
  if (!solucoes.some((s) => s.id === estadoAcessos.sistema)) {
    estadoAcessos.sistema = (solucoes.find((s) => !s.somente_admin) || solucoes[0]).id;
  }
  const rolagemAntes = alvo.querySelector(".lista-lado-itens")?.scrollTop;
  const itens = h("div", { class: "lista-lado-itens" });
  const pintarLista = () => {
    const grupos = sistemasPorArea(estadoAcessos.buscaSis);
    itens.replaceChildren(...grupos.flatMap(([area, lista]) => [
      h("div", { class: "grupo-titulo" }, area),
      ...lista.map((s) => itemLado(s.id === estadoAcessos.sistema, [
        logoSistema(s, "mini"), h("span", { class: "nome" }, s.nome),
        s.somente_admin ? h("span", { class: "etiqueta-admin" }, "Só administradores")
          : h("span", { class: "contador" }, plural(comAcesso(s.id).filter((u) => u.ativo).length, "pessoa", "pessoas"))],
        () => { estadoAcessos.sistema = s.id; estadoAcessos.filtroQuem = ""; estadoAcessos.todos = false; pintarPorSistema(); })),
    ]));
    if (!grupos.length) itens.append(h("p", { class: "nota vazio-lado" }, "Nenhum sistema encontrado."));
  };
  const busca = h("input", { type: "search", class: "busca", placeholder: "Procurar sistema", "aria-label": "Procurar sistema", value: estadoAcessos.buscaSis,
    oninput: (ev) => { estadoAcessos.buscaSis = ev.target.value; pintarLista(); } });
  pintarLista();
  const sol = solucoes.find((s) => s.id === estadoAcessos.sistema);
  alvo.replaceChildren(h("div", { class: "duas-colunas" },
    h("section", { class: "painel lista-lado", "aria-label": "Sistemas" }, h("div", { class: "busca-lado" }, busca), itens),
    detalheSistema(sol)));
  manterRolagem(itens, rolagemAntes);
}

function detalheSistema(sol) {
  const admins = adminsAtivos();
  const cabeca = h("div", { class: "cabeca-detalhe" }, logoSistema(sol, "medio"),
    h("div", {}, h("h2", {}, sol.nome),
      h("p", {}, sol.somente_admin
        ? `${sol.categoria || "Geral"} · só administradores do portal abrem (${plural(admins.length, "administrador", "administradores")})`
        : `${sol.categoria || "Geral"} · ${plural(comAcesso(sol.id).filter((u) => u.ativo).length, "pessoa com acesso", "pessoas com acesso")}, além de ${plural(admins.length, "administrador", "administradores")}`)));
  if (sol.aberto) {
    cabeca.append(h("p", { class: "nota" }, "Acesso direto: o endereço abre sem o login do portal, com o login do próprio sistema. A liberação abaixo só decide quem o vê em Meus sistemas."));
  }
  if (sol.somente_admin) {
    return h("section", { class: "painel detalhe-acesso" }, cabeca,
      h("p", { class: "nota" }, "Este sistema dá acesso total a bancos de dados. Não pode ser liberado para usuários: para dar acesso, torne a pessoa administradora em Usuários."),
      h("table", { class: "tabela-pessoas" }, h("tbody", {}, admins.map((u) => linhaPessoa(u, null)))));
  }

  // adicionar pessoa: busca com sugestões
  const candidatos = () => ativosComuns().filter((u) => !u.solucoes.includes(sol.id));
  const campo = h("input", { type: "search", class: "busca", placeholder: "Adicionar pessoa: nome ou usuário", "aria-label": "Adicionar pessoa",
    autocomplete: "off", role: "combobox", "aria-expanded": "false", "aria-controls": "sugestoes-acesso" });
  const caixa = h("div", { class: "sugestoes", id: "sugestoes-acesso", role: "listbox", hidden: true });
  let opcoes = [];
  let foco = 0;
  const pintarSugestoes = () => {
    const termo = normalizar(campo.value.trim());
    if (!termo) { caixa.hidden = true; campo.setAttribute("aria-expanded", "false"); return; }
    const todos = estadoUsuarios.dados.usuarios.filter((u) => u.ativo && normalizar(`${u.nome} ${u.usuario}`).includes(termo));
    opcoes = todos.slice(0, 8);
    foco = Math.max(0, opcoes.findIndex((u) => ehUsuarioComum(u) && !u.solucoes.includes(sol.id)));
    caixa.replaceChildren(...(opcoes.length ? opcoes.map((u, i) => {
      const bloqueio = !ehUsuarioComum(u) ? "administrador: já acessa" : u.solucoes.includes(sol.id) ? "já tem acesso" : "";
      return h("div", { role: "option", class: `${i === foco ? "foco" : ""}${bloqueio ? " desligada" : ""}`, "aria-selected": String(i === foco),
        onmousedown: (ev) => { ev.preventDefault(); if (!bloqueio) adicionar(u); } },
        avatar(u, "pequeno"), h("span", {}, u.nome), h("small", {}, u.usuario + (bloqueio ? ` · ${bloqueio}` : "")));
    }) : [h("p", { class: "nota" }, "Ninguém encontrado.")]));
    caixa.hidden = false;
    campo.setAttribute("aria-expanded", "true");
  };
  const adicionar = async (u) => {
    try {
      await mudarAcesso([u.id], [sol.id], true);
      toast(`${sol.nome} liberado para ${u.nome}.`);
      campo.value = "";
      repintarAcessos();
      document.querySelector(".adicionar input")?.focus();
    } catch (e) { toast(e.message, "erro"); }
  };
  campo.addEventListener("input", pintarSugestoes);
  campo.addEventListener("blur", () => setTimeout(() => { caixa.hidden = true; }, 120));
  campo.addEventListener("keydown", (ev) => {
    if (caixa.hidden || !opcoes.length) return;
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      foco = (foco + (ev.key === "ArrowDown" ? 1 : opcoes.length - 1)) % opcoes.length;
      [...caixa.children].forEach((c, i) => { c.classList.toggle("foco", i === foco); c.setAttribute("aria-selected", String(i === foco)); });
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      const u = opcoes[foco];
      if (u && ehUsuarioComum(u) && !u.solucoes.includes(sol.id)) adicionar(u);
    } else if (ev.key === "Escape") { caixa.hidden = true; }
  });
  const botaoAdicionar = h("button", { type: "button", class: "botao primario", onclick: () => {
    const u = opcoes[foco];
    if (!caixa.hidden && u && ehUsuarioComum(u) && !u.solucoes.includes(sol.id)) adicionar(u);
    else { campo.focus(); toast("Digite um nome e escolha a pessoa na lista."); }
  } }, "Adicionar");

  // quem tem acesso
  const quem = comAcesso(sol.id).sort((a, b) => (b.ativo - a.ativo) || a.nome.localeCompare(b.nome, "pt-BR"));
  const termo = normalizar(estadoAcessos.filtroQuem);
  const filtrados = quem.filter((u) => !termo || normalizar(`${u.nome} ${u.usuario}`).includes(termo));
  const LIMITE = 50;
  const mostrar = estadoAcessos.todos ? filtrados : filtrados.slice(0, LIMITE);
  const corpo = h("tbody", {}, mostrar.map((u) => linhaPessoa(u, async () => {
    try {
      await mudarAcesso([u.id], [sol.id], false);
      toast(`${sol.nome} retirado de ${u.nome}.`);
      repintarAcessos();
    } catch (e) { toast(e.message, "erro"); }
  })));
  const filtro = h("input", { type: "search", class: "busca", placeholder: "Filtrar quem tem acesso", "aria-label": "Filtrar quem tem acesso", value: estadoAcessos.filtroQuem,
    oninput: (ev) => {
      estadoAcessos.filtroQuem = ev.target.value;
      const pos = ev.target.selectionStart;
      repintarAcessos();
      const novo = document.querySelector(".detalhe-acesso .filtro-quem input");
      if (novo) { novo.focus(); novo.setSelectionRange(pos, pos); }
    } });

  return h("section", { class: "painel detalhe-acesso" }, cabeca,
    h("div", { class: "adicionar" }, h("div", { class: "campo-busca" }, campo, caixa), botaoAdicionar),
    h("p", { class: "atalhos" }, "Ou: ",
      h("button", { type: "button", class: "link-acao", onclick: () => dialogoColarLista(sol) }, "colar uma lista de usuários"), " · ",
      h("button", { type: "button", class: "link-acao", onclick: () => dialogoCopiarSistema(sol) }, "copiar o acesso de outro sistema"),
      quem.length ? [" · ", h("button", { type: "button", class: "link-acao perigo", onclick: () => retirarTodos(sol, quem) }, "retirar de todos")] : null),
    quem.length
      ? [quem.length > 8 ? h("div", { class: "filtro-quem" }, filtro) : null,
        h("table", { class: "tabela-pessoas" }, corpo),
        !mostrar.length ? h("p", { class: "nota" }, "Ninguém encontrado com esse filtro.") : null,
        filtrados.length > mostrar.length ? h("p", { class: "nota" }, `Mostrando ${mostrar.length} de ${filtrados.length}. `,
          h("button", { type: "button", class: "link-acao", onclick: () => { estadoAcessos.todos = true; repintarAcessos(); } }, "Ver todas")) : null]
      : h("p", { class: "nota" }, candidatos().length
        ? "Ninguém tem acesso ainda (fora os administradores). Adicione as pessoas acima."
        : "Nenhum usuário ativo para liberar. Crie as pessoas em Usuários."));
}

function linhaPessoa(u, retirar) {
  return h("tr", { class: u.ativo ? "" : "inativo" },
    h("td", {}, h("div", { class: "pessoa" }, avatar(u, "pequeno"),
      h("div", {}, h("span", { class: "pessoa-nome" }, u.nome), h("div", { class: "sub" }, u.usuario)))),
    h("td", { class: "sub" }, !u.ativo ? "Desativado (não entra no portal)"
      : u.trocar_senha ? "Ainda não fez o primeiro acesso" : `Último acesso ao portal: ${quando(u.ultimo_login, true).toLowerCase()}`),
    h("td", { class: "acao" }, retirar ? h("button", { type: "button", class: "link-retirar", onclick: retirar }, "Retirar") : null));
}

async function retirarTodos(sol, quem) {
  if (!(await confirmar({ titulo: `Retirar ${sol.nome} de todos?`,
    texto: `Retira o acesso de ${plural(quem.length, "pessoa", "pessoas")}. Os administradores continuam acessando.`,
    botao: "Retirar de todos", perigo: true }))) return;
  try {
    await mudarAcesso(quem.map((u) => u.id), [sol.id], false);
    toast(`Acesso retirado de ${plural(quem.length, "pessoa", "pessoas")}.`);
    repintarAcessos();
  } catch (e) { toast(e.message, "erro"); }
}

/** Cola usuários (um por linha, ou separados por vírgula/ponto e vírgula) e libera para todos de uma vez. */
function dialogoColarLista(sol) {
  const texto = h("textarea", { id: "cl-lista", rows: 8, spellcheck: false, placeholder: "ana.lima\nbruno.carvalho\nCarla Mendes" });
  const previa = h("div", { class: "nota" });
  const botao = h("button", { type: "button", class: "botao primario", disabled: true }, "Liberar");
  let achados = [];
  const analisar = () => {
    const termos = [...new Set(texto.value.split(/[\n,;\t]+/).map((t) => t.trim()).filter(Boolean))];
    const porUsuario = new Map(estadoUsuarios.dados.usuarios.map((u) => [u.usuario.toLowerCase(), u]));
    const porNome = new Map(estadoUsuarios.dados.usuarios.map((u) => [normalizar(u.nome), u]));
    achados = []; const naoAchados = []; const ignorados = [];
    for (const t of termos) {
      const u = porUsuario.get(t.toLowerCase()) || porNome.get(normalizar(t));
      if (!u) naoAchados.push(t);
      else if (!u.ativo || !ehUsuarioComum(u) || u.solucoes.includes(sol.id)) ignorados.push(`${u.usuario} (${!u.ativo ? "desativado" : !ehUsuarioComum(u) ? "administrador" : "já tem acesso"})`);
      else if (!achados.includes(u)) achados.push(u);
    }
    previa.replaceChildren(
      termos.length ? h("p", {}, h("strong", {}, plural(achados.length, "pessoa recebe", "pessoas recebem")), " o acesso.") : null,
      ignorados.length ? h("p", {}, `Ficam como estão: ${ignorados.join(", ")}.`) : null,
      naoAchados.length ? h("p", { class: "form-erro" }, `Não encontrados no portal: ${naoAchados.join(", ")}. Crie em Usuários ou confira a grafia.`) : null);
    botao.disabled = !achados.length;
    botao.textContent = achados.length ? `Liberar para ${plural(achados.length, "pessoa", "pessoas")}` : "Liberar";
  };
  texto.addEventListener("input", analisar);
  botao.addEventListener("click", async () => {
    botao.disabled = true;
    try {
      await mudarAcesso(achados.map((u) => u.id), [sol.id], true);
      dlg.close();
      toast(`${sol.nome} liberado para ${plural(achados.length, "pessoa", "pessoas")}.`);
      repintarAcessos();
    } catch (e) { toast(e.message, "erro"); botao.disabled = false; }
  });
  const dlg = dialogo({ titulo: `Liberar ${sol.nome} para uma lista`, descricao: "Um usuário (ou nome completo) por linha, ou separados por vírgula. Dá para colar uma coluna do Excel.",
    corpo: [h("div", { class: "campo" }, h("label", { for: "cl-lista" }, "Usuários"), texto), previa],
    rodape: [h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"), botao] });
}

function dialogoCopiarSistema(sol) {
  const outros = estadoUsuarios.dados.solucoes.filter((s) => s.id !== sol.id && !s.somente_admin);
  const sel = h("select", { id: "cs-origem" }, outros.map((s) => h("option", { value: s.id }, `${s.nome} (${plural(comAcesso(s.id).filter((u) => u.ativo).length, "pessoa", "pessoas")})`)));
  const previa = h("p", { class: "nota" });
  const botao = h("button", { type: "button", class: "botao primario" }, "Liberar");
  const novos = () => ativosComuns().filter((u) => u.solucoes.includes(sel.value) && !u.solucoes.includes(sol.id));
  const atualizar = () => {
    const n = novos().length;
    previa.textContent = n ? `${plural(n, "pessoa que usa", "pessoas que usam")} o sistema escolhido e ainda não ${n === 1 ? "tem" : "têm"} ${sol.nome} ${n === 1 ? "vai receber" : "vão receber"} o acesso. Ninguém perde acesso.`
      : `Todas as pessoas do sistema escolhido já têm ${sol.nome}.`;
    botao.disabled = !n;
    botao.textContent = n ? `Liberar para ${plural(n, "pessoa", "pessoas")}` : "Liberar";
  };
  sel.addEventListener("change", atualizar);
  botao.addEventListener("click", async () => {
    const lista = novos();
    botao.disabled = true;
    try {
      await mudarAcesso(lista.map((u) => u.id), [sol.id], true);
      dlg.close();
      toast(`${sol.nome} liberado para ${plural(lista.length, "pessoa", "pessoas")}.`);
      repintarAcessos();
    } catch (e) { toast(e.message, "erro"); botao.disabled = false; }
  });
  const dlg = dialogo({ titulo: `Copiar o acesso de outro sistema para ${sol.nome}`,
    corpo: outros.length ? [h("div", { class: "campo" }, h("label", { for: "cs-origem" }, "Quem usa este sistema"), sel), previa]
      : h("p", { class: "nota" }, "Não há outro sistema para copiar."),
    rodape: [h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"), outros.length ? botao : null] });
  if (outros.length) atualizar();
}

/* ---- aba "Por pessoa" ---- */

function pintarPorPessoa() {
  const alvo = document.getElementById("acessos-corpo");
  if (!alvo) return;
  const { usuarios } = estadoUsuarios.dados;
  const visiveis = () => {
    const termo = normalizar(estadoAcessos.buscaPes);
    return usuarios.filter((u) => (estadoAcessos.inativos || u.ativo) && (!termo || normalizar(`${u.nome} ${u.usuario}`).includes(termo)))
      .sort((a, b) => (a.perfil === "admin") - (b.perfil === "admin") || (b.ativo - a.ativo) || a.nome.localeCompare(b.nome, "pt-BR"));
  };
  if (!usuarios.some((u) => u.id === estadoAcessos.pessoa)) {
    estadoAcessos.pessoa = (visiveis().find(ehUsuarioComum) || usuarios[0])?.id ?? null;
  }
  const rolagemAntes = alvo.querySelector(".lista-lado-itens")?.scrollTop;
  const itens = h("div", { class: "lista-lado-itens" });
  const pintarLista = () => {
    const lista = visiveis();
    const comuns = lista.filter(ehUsuarioComum);
    const adms = lista.filter((u) => !ehUsuarioComum(u));
    const item = (u) => itemLado(u.id === estadoAcessos.pessoa, [
      avatar(u, "pequeno"), h("span", { class: "nome" }, u.nome, h("span", { class: "sub" }, u.usuario + (u.ativo ? "" : " · desativado"))),
      ehUsuarioComum(u) ? h("span", { class: "contador" }, plural(u.solucoes.length, "sistema", "sistemas")) : h("span", { class: "etiqueta-admin" }, "Todos")],
      () => { estadoAcessos.pessoa = u.id; pintarPorPessoa(); });
    itens.replaceChildren(
      comuns.length ? h("div", { class: "grupo-titulo" }, `Pessoas · ${comuns.length}`) : null, ...comuns.map(item),
      adms.length ? h("div", { class: "grupo-titulo" }, `Administradores · ${adms.length}`) : null, ...adms.map(item),
      lista.length ? null : h("p", { class: "nota vazio-lado" }, "Ninguém encontrado."));
  };
  const busca = h("input", { type: "search", class: "busca", placeholder: "Procurar pessoa", "aria-label": "Procurar pessoa", value: estadoAcessos.buscaPes,
    oninput: (ev) => { estadoAcessos.buscaPes = ev.target.value; pintarLista(); } });
  const inativos = h("label", { class: "campo-check compacto" },
    h("input", { type: "checkbox", checked: estadoAcessos.inativos, onchange: (ev) => { estadoAcessos.inativos = ev.target.checked; pintarLista(); } }),
    h("span", {}, "Mostrar desativados"));
  pintarLista();
  const u = usuarios.find((x) => x.id === estadoAcessos.pessoa);
  alvo.replaceChildren(h("div", { class: "duas-colunas" },
    h("section", { class: "painel lista-lado", "aria-label": "Pessoas" }, h("div", { class: "busca-lado" }, busca, inativos), itens),
    u ? detalhePessoa(u) : h("section", { class: "painel" }, h("p", { class: "nota" }, "Nenhuma pessoa cadastrada. Crie em Usuários."))));
  manterRolagem(itens, rolagemAntes);
}

function detalhePessoa(u) {
  const cabeca = h("div", { class: "cabeca-detalhe" }, avatar(u, "grande"),
    h("div", {}, h("h2", {}, u.nome),
      h("p", {}, [u.usuario, ehUsuarioComum(u) ? "usuário" : "administrador",
        !u.ativo ? "desativado" : u.trocar_senha ? "ainda não fez o primeiro acesso" : `último acesso ${quando(u.ultimo_login, true).toLowerCase()}`].join(" · "))),
    ehUsuarioComum(u) ? h("div", { class: "lado-direito" },
      h("button", { type: "button", class: "botao", onclick: () => dialogoCopiarPessoa(u) }, "Copiar acessos de outra pessoa")) : null);
  if (!ehUsuarioComum(u)) {
    return h("section", { class: "painel detalhe-acesso" }, cabeca,
      h("p", { class: "nota" }, "Administradores acessam todos os sistemas, inclusive os “só administradores”. Para limitar, mude o perfil para usuário em Usuários."));
  }
  const nomes = new Map(estadoUsuarios.dados.solucoes.map((s) => [s.id, s.nome]));
  const resumo = u.solucoes.length
    ? h("div", { class: "resumo-pessoa" }, u.solucoes.map((sid) => h("span", { class: "chip" }, nomes.get(sid) || sid)))
    : h("p", { class: "nota" }, "Nenhum sistema liberado ainda.");
  const grade = sistemasPorArea().map(([area, lista]) => h("div", { class: "area" }, h("h3", {}, area),
    h("div", { class: "grade-sistemas" }, lista.map((s) => {
      if (s.somente_admin) {
        return h("div", { class: "cartao-sistema bloqueado", title: "Só administradores do portal abrem este sistema" },
          logoSistema(s, "mini"), h("span", { class: "n" }, s.nome), h("span", { class: "etiqueta-admin" }, "Só administradores"));
      }
      const marcado = u.solucoes.includes(s.id);
      const caixa = h("input", { type: "checkbox", checked: marcado, "aria-label": `${s.nome} para ${u.nome}` });
      caixa.addEventListener("change", async () => {
        caixa.disabled = true;
        try {
          await mudarAcesso([u.id], [s.id], caixa.checked);
          toast(`${s.nome} ${caixa.checked ? "liberado para" : "retirado de"} ${u.nome}.`);
          repintarAcessos();
        } catch (e) { caixa.checked = !caixa.checked; caixa.disabled = false; toast(e.message, "erro"); }
      });
      return h("label", { class: `cartao-sistema${marcado ? " marcado" : ""}` }, caixa, logoSistema(s, "mini"), h("span", { class: "n" }, s.nome));
    }))));
  return h("section", { class: "painel detalhe-acesso" }, cabeca, resumo,
    !u.ativo ? h("p", { class: "nota" }, "Pessoa desativada: não entra no portal. Os acessos ficam guardados para quando for reativada.") : null,
    grade, h("p", { class: "nota" }, "Marcar ou desmarcar vale na hora e fica no histórico."));
}

/** Acrescenta a esta pessoa os sistemas de outra (ninguém perde acesso). */
function dialogoCopiarPessoa(u) {
  const outras = ativosComuns().filter((x) => x.id !== u.id && x.solucoes.length).sort((a, b) => a.nome.localeCompare(b.nome, "pt-BR"));
  const sel = h("select", { id: "cp-origem" }, outras.map((x) => h("option", { value: String(x.id) }, `${x.nome} (${x.usuario}) · ${plural(x.solucoes.length, "sistema", "sistemas")}`)));
  const previa = h("div", { class: "nota" });
  const botao = h("button", { type: "button", class: "botao primario" }, "Copiar");
  // Só os sistemas que se liberam (os "só administradores" ficam de fora).
  const nomes = new Map(estadoUsuarios.dados.solucoes.filter((s) => !s.somente_admin).map((s) => [s.id, s.nome]));
  const novos = () => {
    const origem = outras.find((x) => String(x.id) === sel.value);
    return origem ? origem.solucoes.filter((s) => !u.solucoes.includes(s) && nomes.has(s)) : [];
  };
  const atualizar = () => {
    const n = novos();
    previa.replaceChildren(n.length
      ? h("p", {}, `${u.nome} recebe: `, h("strong", {}, n.map((s) => nomes.get(s)).join(", ")), ". O que a pessoa já tem continua.")
      : h("p", {}, `${u.nome} já tem todos os sistemas dessa pessoa.`));
    botao.disabled = !n.length;
  };
  sel.addEventListener("change", atualizar);
  botao.addEventListener("click", async () => {
    const lista = novos();
    botao.disabled = true;
    try {
      await mudarAcesso([u.id], lista, true);
      dlg.close();
      toast(`${plural(lista.length, "sistema liberado", "sistemas liberados")} para ${u.nome}.`);
      repintarAcessos();
    } catch (e) { toast(e.message, "erro"); botao.disabled = false; }
  });
  const dlg = dialogo({ titulo: `Copiar acessos para ${u.nome}`,
    corpo: outras.length ? [h("div", { class: "campo" }, h("label", { for: "cp-origem" }, "Copiar os sistemas de"), sel), previa]
      : h("p", { class: "nota" }, "Nenhuma outra pessoa com sistemas liberados."),
    rodape: [h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"), outras.length ? botao : null] });
  if (outras.length) atualizar();
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

/* ================= admin: assistente "Nova solução" ================= */

function baixarArquivosLocais(id) {
  // Um .zip com a pasta solucoes/<id>/ pronta para o repositório do portal (um download só).
  const a = h("a", { href: `/api/solucoes/${encodeURIComponent(id)}/arquivos-locais.zip`, download: `solucao-${id}.zip`, hidden: true });
  document.body.append(a);
  a.click();
  setTimeout(() => a.remove(), 1000);
}

async function removerLocal(sol) {
  if (!(await confirmar({
    titulo: `Remover ${sol.nome} do catálogo?`,
    texto: "O manifesto criado automaticamente é apagado deste servidor. Antes, pare e remova o container. Os volumes e o banco da solução não são apagados.",
    botao: "Remover do catálogo", perigo: true,
  }))) return;
  try {
    await api(`/api/solucoes/${sol.id}/local`, { method: "DELETE" });
    toast(`${sol.nome} removida do catálogo.`);
    location.hash = "#/admin/solucoes";
  } catch (e) { toast(e.message, "erro"); }
}

const NIVEIS = { erro: "Resolver antes de usar com dados reais", atencao: "Atenção", info: "Para saber" };

/** "A=1\nB=2" <-> {A: "1", B: "2"} */
function paraLinhas(obj) { return Object.entries(obj || {}).map(([k, v]) => `${k}=${v}`).join("\n"); }
function deLinhas(texto) {
  const obj = {};
  for (const linha of texto.split("\n")) {
    const i = linha.indexOf("=");
    if (i > 0) obj[linha.slice(0, i).trim()] = linha.slice(i + 1).trim();
  }
  return obj;
}

function telaNovaSolucao() {
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const form = h("form", { class: "config" },
    h("div", { class: "campo" }, h("label", { for: "ns-repo" }, "Repositório Git"),
      h("input", { id: "ns-repo", name: "repositorio", type: "text", required: true, autocomplete: "off",
        placeholder: "https://github.com/empresa/projeto.git" }),
      h("small", {}, "Endereço HTTPS. Se for privado, o repositório precisa estar no token de leitura deste servidor.")),
    h("div", { class: "form-grade" },
      h("div", { class: "campo" }, h("label", { for: "ns-ref" }, "Branch ou tag"),
        h("input", { id: "ns-ref", name: "ref", type: "text", value: "main", autocomplete: "off" })),
      h("div", { class: "campo" }, h("label", { for: "ns-sub" }, "Subpasta (opcional)"),
        h("input", { id: "ns-sub", name: "subpasta", type: "text", autocomplete: "off", placeholder: "ex.: backend" }),
        h("small", {}, "Quando o sistema fica numa pasta dentro do repositório."))),
    erro,
    h("div", { class: "linha-botoes" }, h("button", { type: "submit", class: "botao primario" }, "Analisar repositório")));
  const resultado = h("div", { id: "ns-resultado" });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    erro.hidden = true;
    const botao = form.querySelector("button[type=submit]");
    botao.disabled = true;
    botao.replaceChildren(h("span", { class: "giro", "aria-hidden": "true" }), "Baixando e analisando…");
    resultado.replaceChildren(h("p", { class: "nota" }, "Baixando o código e procurando a tecnologia. Pode demorar em repositórios grandes."));
    try {
      const an = await api("/api/assistente/analisar", {
        method: "POST", body: { repositorio: form.repositorio.value.trim(), ref: form.ref.value.trim() || "main", subpasta: form.subpasta.value.trim() },
      });
      pintarAnalise(resultado, an);
      resultado.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      resultado.replaceChildren();
      erro.textContent = e.message;
      erro.hidden = false;
    } finally {
      botao.disabled = false;
      botao.textContent = "Analisar novamente";
    }
  });
  $conteudo.replaceChildren(
    h("a", { class: "voltar", href: "#/admin/solucoes" }, "Todas as soluções"),
    h("section", { class: "intro" },
      h("h1", {}, "Nova solução"),
      h("p", {}, "Informe o repositório: o portal baixa o código, identifica a tecnologia, prepara o Dockerfile e aponta o que precisa de ajuste. Nada é instalado sem confirmação.")),
    h("section", { class: "painel estreito" }, h("h2", {}, "1. Repositório"), form),
    resultado);
  form.repositorio.focus();
}

function painelAvisos(avisos) {
  return avisos.length
    ? h("ul", { class: "avisos-lista" }, avisos.map((a) => h("li", { class: `aviso-item ${a.nivel}` },
        h("span", { class: "aviso-nivel" }, NIVEIS[a.nivel] || a.nivel),
        h("strong", {}, a.titulo),
        h("p", {}, a.detalhe),
        a.arquivos.length ? h("p", { class: "aviso-arquivos" }, a.arquivos.map((f) => h("code", {}, f))) : null)))
    : h("p", { class: "texto-ok" }, "Nenhum problema encontrado.");
}

function painelDeteccao(an, titulo) {
  const o = an.origem;
  return h("section", { class: "painel" },
    h("h2", {}, titulo),
    h("div", { class: "deteccao" },
      h("div", {},
        h("span", { class: "deteccao-rotulo" }, "Tecnologia"),
        h("strong", { class: "deteccao-valor" }, an.nome_tecnologia),
        h("small", {}, an.evidencias.length ? `Pelos arquivos: ${an.evidencias.join(", ")}` : "")),
      h("div", {},
        h("span", { class: "deteccao-rotulo" }, "Como irá rodar"),
        h("strong", { class: "deteccao-valor" }, an.comando || "—"),
        h("small", {}, an.tem_dockerfile ? "Usa o Dockerfile do repositório." : an.dockerfile ? "Dockerfile gerado pelo portal (revise abaixo)." : "")),
      h("div", {},
        h("span", { class: "deteccao-rotulo" }, "Código"),
        h("strong", { class: "deteccao-valor" }, `${o.ref} · ${o.commit}`),
        h("small", {}, o.resumo_commit))),
    h("h3", { class: "topo-16" }, "Pontos de atenção"), painelAvisos(an.avisos));
}

/** Valores do formulário a partir de uma análise nova. */
function valoresDaAnalise(an) {
  return {
    repositorio: an.origem.repositorio, ref: an.origem.ref, subpasta: an.origem.subpasta, tecnologia: an.nome_tecnologia,
    id: an.id_sugerido, nome: an.nome_sugerido, resumo: an.resumo_sugerido, categoria: "Geral",
    porta_host: an.porta_host, porta_container: an.porta_container, caminho_saude: an.caminho_saude,
    ambiente: { ...an.ambiente }, volumes: { ...an.volumes }, segredos: [...an.segredos],
    configuracoes: an.configuracoes.map((c) => ({ ...c, incluir: true })),
    banco: an.banco, usar_banco: !!an.banco,
    supabase: an.supabase, usar_supabase: !!an.supabase, supabase_realtime: !!an.supabase_realtime,
    tem_dockerfile: an.tem_dockerfile, dockerfile: an.dockerfile, arquivos: { ...(an.arquivos || {}) },
    variaveis_na_construcao: an.tecnologia === "spa",
  };
}

/** Variáveis que os empacotadores de frontend só leem ao compilar (iguais às do portal). */
const PREFIXO_CONSTRUCAO = /^(VITE_|REACT_APP_|NEXT_PUBLIC_|VUE_APP_|NUXT_PUBLIC_|PUBLIC_|EXPO_PUBLIC_)/;

/** Tabela das configurações que o sistema lê: cada uma vira um campo em Configurações. */
function tabelaConfiguracoes(lista, banco) {
  const corpo = h("tbody");
  const linhas = [];
  const doBanco = new Set(Object.keys(banco || {}));
  const novaLinha = (c) => {
    const incluir = h("input", { type: "checkbox", checked: c.incluir !== false, "aria-label": `Incluir ${c.nome || "configuração"}` });
    const nome = h("input", { type: "text", value: c.nome || "", "aria-label": "Nome da variável", class: "cfg-nome", spellcheck: false, placeholder: "NOME_DA_VARIAVEL" });
    const tipo = h("select", { "aria-label": "Tipo" },
      [["texto", "Texto"], ["segredo", "Segredo"], ["booleano", "Sim/não"]].map(([v, r]) => h("option", { value: v, selected: (c.tipo || "texto") === v }, r)));
    const padrao = h("input", { type: "text", value: c.padrao || "", "aria-label": "Valor padrão", placeholder: "sem padrão" });
    const obrig = h("input", { type: "checkbox", checked: !!c.obrigatoria, "aria-label": "Obrigatória" });
    const constr = h("input", { type: "checkbox", checked: !!c.construcao || PREFIXO_CONSTRUCAO.test(c.nome || ""),
      "aria-label": "Incluído na criação", title: "Incluído no sistema na criação (Vite, React, Next...): a alteração é aplicada depois de Atualizar" });
    nome.addEventListener("input", () => { if (PREFIXO_CONSTRUCAO.test(nome.value.trim())) constr.checked = true; });
    const sincronizar = () => { padrao.disabled = tipo.value === "segredo"; if (padrao.disabled) padrao.value = ""; };
    tipo.addEventListener("change", sincronizar);
    sincronizar();
    const tr = h("tr", { class: c.detectada ? "detectada" : "" },
      h("td", { class: "col-check" }, incluir),
      h("td", {}, nome, c.ajuda ? h("small", { class: "cfg-ajuda" }, c.ajuda) : null,
        doBanco.has(c.nome) ? h("small", { class: "cfg-ajuda" }, "Preenchida com o banco do servidor.") : null),
      h("td", {}, tipo), h("td", {}, padrao), h("td", { class: "col-check" }, obrig), h("td", { class: "col-check" }, constr));
    linhas.push({ c, incluir, nome, tipo, padrao, obrig, constr, tr });
    corpo.append(tr);
  };
  lista.forEach(novaLinha);
  const el = h("div", { class: "campo" },
    h("label", {}, "Configurações lidas pelo sistema"),
    h("small", { class: "sem-topo" }, "Cada linha se torna um campo da solução em Gerenciar → Configurações, onde o administrador pode alterar o valor. É Obrigatória quando o sistema não inicia sem ela, e Construção quando o valor é gravado no sistema quando ele é criado (Vite, React, Next): nesse caso, os valores só são aplicados depois de Atualizar."),
    h("div", { class: "tabela-rolagem limite" },
      h("table", { class: "tabela-simples tabela-cfg" },
        h("thead", {}, h("tr", {}, ["Usar", "Variável", "Tipo", "Valor padrão", "Obrig.", "Construção"].map((x) => h("th", {}, x)))),
        corpo)),
    h("div", { class: "linha-botoes topo-8" },
      h("button", { type: "button", class: "botao pequeno", onclick: () => { novaLinha({ nome: "", tipo: "texto", incluir: true }); linhas.at(-1).nome.focus(); } }, "Adicionar configuração")));
  const valores = () => linhas
    .filter((l) => l.incluir.checked && l.nome.value.trim())
    .map((l) => ({ nome: l.nome.value.trim(), rotulo: l.c.rotulo && l.c.nome === l.nome.value.trim() ? l.c.rotulo : l.nome.value.trim(),
                   tipo: l.tipo.value, padrao: l.padrao.value, ajuda: l.c.ajuda || "", obrigatoria: l.obrig.checked,
                   construcao: l.constr.checked }));
  const adicionar = (c) => novaLinha({ ...c, incluir: true });
  const nomes = () => new Set(linhas.map((l) => l.nome.value.trim()));
  const semConstrucao = () => new Set(linhas.filter((l) => !l.constr.checked).map((l) => l.nome.value.trim()));
  const marcarConstrucao = (lista) => { for (const l of linhas) if (lista.includes(l.nome.value.trim())) l.constr.checked = true; };
  return { el, valores, adicionar, nomes, semConstrucao, marcarConstrucao };
}

/** Formulário da solução (criar ou editar). */
function formularioSolucao(v, { modo, aoSalvar }) {
  const campo = (id, rotulo, valor, extra = {}, ajuda = "") => h("div", { class: "campo" },
    h("label", { for: id }, rotulo),
    h("input", { id, name: id, type: extra.type || "text", value: valor ?? "", autocomplete: "off", ...extra }),
    ajuda ? h("small", {}, ajuda) : null);
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  let arquivos = { ...(v.arquivos || {}) };
  const dockerfile = v.tem_dockerfile && !v.dockerfile ? null : h("textarea", { id: "ns-dockerfile", class: "codigo", rows: 16, spellcheck: false }, v.dockerfile || "");
  const notaArquivos = h("span", {});
  const pintarNota = () => notaArquivos.replaceChildren(Object.keys(arquivos).length
    ? ` Juntamente irão os arquivos auxiliares ${Object.keys(arquivos).join(", ")}.` : "");
  pintarNota();
  const tabela = tabelaConfiguracoes(v.configuracoes || [], v.usar_banco ? v.banco : null);
  const usarBanco = v.banco ? h("label", { class: "campo-check" },
    h("input", { type: "checkbox", id: "ns-banco", checked: v.usar_banco }),
    h("span", {}, h("strong", {}, "Usar o banco PostgreSQL do servidor"),
      h("small", {}, ` O projeto lê ${Object.keys(v.banco).join(", ")}: o portal cria um banco só para ele e preenche a conexão.`))) : null;

  const sbv = v.supabase || {};
  const campoSb = (campo, rotulo, ajuda) => h("div", { class: "campo" },
    h("label", { for: `ns-sb-${campo}` }, rotulo),
    h("input", { id: `ns-sb-${campo}`, type: "text", value: sbv[campo] || "", spellcheck: false, class: "cfg-nome", placeholder: "não usa" }),
    h("small", {}, ajuda));
  const usarSupabase = v.supabase || v.usar_supabase ? h("div", { class: "grupo-supabase" },
    h("label", { class: "campo-check" },
      h("input", { type: "checkbox", id: "ns-supabase", checked: !!v.usar_supabase }),
      h("span", {}, h("strong", {}, "Criar um Supabase só para este sistema"),
        h("small", {}, " Banco, login e arquivos próprios, no servidor, no mesmo endereço do sistema. O portal preenche as variáveis abaixo sozinho. O banco inicia vazio."))),
    h("div", { class: "form-grade" },
      campoSb("url", "Variável do endereço", "Recebe o endereço do sistema (ex.: VITE_SUPABASE_URL)."),
      campoSb("chave_anon", "Variável da chave pública", "Recebe a chave anon (ex.: VITE_SUPABASE_ANON_KEY)."),
      campoSb("chave_servico", "Chave de serviço (opcional)", "Só para código que roda no servidor. Nunca deve ser usada em um site estático.")),
    h("label", { class: "campo-check" },
      h("input", { type: "checkbox", id: "ns-sb-realtime", checked: !!v.supabase_realtime }),
      h("span", {}, h("strong", {}, "Tempo real (Realtime)"),
        h("small", {}, " Para telas que atualizam sozinhas (supabase-js .channel()). Usa cerca de 200 MB de memória a mais."))))
    : null;

  const form = h("form", { class: "config", id: "ns-form" },
    h("div", { class: "form-grade" },
      campo("ns-nome", "Nome", v.nome, { required: true, maxlength: 120 }),
      campo("ns-id", "Identificador", v.id, modo === "editar" ? { disabled: true } : { required: true, pattern: "[a-z0-9][a-z0-9\\-]{1,40}" },
        modo === "editar" ? "Não é alterado depois de criado." : "Minúsculas e hífens. Vira o nome do container. Não é alterado posteriormente."),
      campo("ns-categoria", "Área", v.categoria, { maxlength: 60 }, "Ex.: Recursos Humanos, Operação, Financeiro.")),
    campo("ns-resumo", "Resumo", v.resumo, { maxlength: 200 }, "Uma frase sobre o que o sistema faz. Aparece para os usuários em Meus sistemas."),
    h("div", { class: "form-grade" },
      campo("ns-porta-host", "Porta pública", v.porta_host, { type: "number" }, modo === "editar" ? "Mudar a porta muda o endereço da solução." : "A primeira livre da faixa do servidor."),
      campo("ns-porta", "Porta da aplicação", v.porta_container, { type: "number" }, "Onde o sistema é acessado dentro do container."),
      campo("ns-saude", "Rota de saúde", v.caminho_saude, {}, "O portal testa esta rota para saber se está no ar.")),
    usarBanco,
    usarSupabase,
    h("label", { class: "campo-check" },
      h("input", { type: "checkbox", id: "ns-somente-admin", checked: !!v.somente_admin }),
      h("span", {}, h("strong", {}, "Só administradores"),
        h("small", {}, " Só os administradores do portal abrem este sistema; ele não aparece para liberar em Acessos. Para painéis com acesso total a bancos ou ao servidor."))),
    h("label", { class: "campo-check" },
      h("input", { type: "checkbox", id: "ns-construcao", checked: !!v.variaveis_na_construcao }),
      h("span", {}, h("strong", {}, "Site estático: todas as variáveis entram na construção"),
        h("small", {}, " Para sites Vite, React ou Vue, nada lê variável quando o site está no ar, então as variáveis fixas e todas as configurações vão para o build (momento da criação). Sempre que alterar um valor, clique em Atualizar."))),
    tabela.el,
    dockerfile ? h("div", { class: "campo" },
      h("label", { for: "ns-dockerfile" }, "Dockerfile"),
      dockerfile,
      h("small", {}, "Fica guardado no portal sem alterações no repositório de origem.", notaArquivos))
      : h("p", { class: "nota" }, "Usa o Dockerfile do repositório."),
    h("details", { class: "avancado" },
      h("summary", {}, "Variáveis fixas, volumes e segredos gerados"),
      h("div", { class: "config topo-16" },
        h("div", { class: "campo" }, h("label", { for: "ns-amb" }, "Variáveis de ambiente fixas"),
          h("textarea", { id: "ns-amb", class: "codigo", rows: 4, spellcheck: false }, paraLinhas(v.ambiente)),
          h("small", {}, "Uma por linha, NOME=valor. {url} vira o endereço público e {host}, só o IP ou nome. Não é possível editar depois. Para isso, use a tabela acima.")),
        h("div", { class: "campo" }, h("label", { for: "ns-vol" }, "Pastas que guardam dados (volumes)"),
          h("textarea", { id: "ns-vol", class: "codigo", rows: 2, spellcheck: false }, paraLinhas(v.volumes)),
          h("small", {}, "Uma por linha, nome=/caminho/no/container. O resto do container é refeito a cada atualização.")),
        campo("ns-segredos", "Segredos gerados pelo portal", (v.segredos || []).join(", "), {}, "Nomes separados por vírgula (ex.: SECRET_KEY). Valor aleatório, criado uma vez que nunca aparece na tela."))),
    erro,
    h("div", { class: "linha-botoes" },
      h("button", { type: "submit", class: "botao primario" }, modo === "editar" ? "Salvar alterações" : "Criar solução"),
      modo === "editar" ? h("a", { class: "botao", href: `#/admin/solucao/${v.id}` }, "Cancelar") : null));

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    erro.hidden = true;
    const botao = form.querySelector("button[type=submit]");
    botao.disabled = true;
    const usar = !!form.querySelector("#ns-banco")?.checked;
    const doBanco = Object.keys(v.banco || {});
    const dados = {
      repositorio: v.repositorio, ref: v.ref, subpasta: v.subpasta, tecnologia: v.tecnologia,
      id: modo === "editar" ? v.id : form["ns-id"].value.trim(), nome: form["ns-nome"].value, resumo: form["ns-resumo"].value,
      categoria: form["ns-categoria"].value,
      porta_host: Number(form["ns-porta-host"].value), porta_container: Number(form["ns-porta"].value), caminho_saude: form["ns-saude"].value.trim(),
      ambiente: deLinhas(form.querySelector("#ns-amb").value), volumes: deLinhas(form.querySelector("#ns-vol").value),
      segredos: form["ns-segredos"].value.split(",").map((s) => s.trim()).filter(Boolean),
      configuracoes: tabela.valores().filter((c) => usar || !doBanco.includes(c.nome)),
      banco: v.banco, usar_banco: usar,
      usar_supabase: !!form.querySelector("#ns-supabase")?.checked,
      supabase_realtime: !!form.querySelector("#ns-sb-realtime")?.checked,
      supabase: form.querySelector("#ns-supabase") ? {
        url: form.querySelector("#ns-sb-url").value.trim(), chave_anon: form.querySelector("#ns-sb-chave_anon").value.trim(),
        chave_servico: form.querySelector("#ns-sb-chave_servico").value.trim(), url_interna: sbv.url_interna || "",
      } : null,
      dockerfile: dockerfile ? dockerfile.value : null,
      arquivos,
      variaveis_na_construcao: !!form.querySelector("#ns-construcao")?.checked,
      somente_admin: !!form.querySelector("#ns-somente-admin")?.checked,
    };
    try { await aoSalvar(dados); } catch (e) { erro.textContent = e.message; erro.hidden = false; botao.disabled = false; }
  });

  /** Aplica o resultado de uma nova análise (Analisar de novo, na edição). */
  const aplicarAnalise = (an) => {
    const atuais = tabela.nomes();
    const novas = an.configuracoes.filter((c) => !atuais.has(c.nome) && !(v.segredos || []).includes(c.nome));
    const dockerNovo = an.dockerfile && dockerfile && an.dockerfile.trim() !== dockerfile.value.trim();
    const sem = tabela.semConstrucao();
    const paraConstrucao = an.configuracoes.filter((c) => c.construcao && atuais.has(c.nome) && sem.has(c.nome)).map((c) => c.nome);
    const siteEstatico = an.tecnologia === "spa" && !form.querySelector("#ns-construcao")?.checked;
    return { novas, dockerNovo, paraConstrucao, siteEstatico,
      marcarConstrucao: () => tabela.marcarConstrucao(paraConstrucao),
      marcarSiteEstatico: () => { form.querySelector("#ns-construcao").checked = true; },
      adicionar: (c) => tabela.adicionar(c),
      usarDockerfile: () => { dockerfile.value = an.dockerfile; arquivos = { ...(an.arquivos || {}) }; pintarNota(); } };
  };
  return { form, aplicarAnalise };
}

function pintarAnalise(alvo, an) {
  const cabecalho = painelDeteccao(an, "2. O que o portal encontrou");
  if (!an.suportado) {
    alvo.replaceChildren(cabecalho, h("div", { class: "vazio" },
      h("strong", {}, "Este projeto não pode ser preparado automaticamente"),
      h("p", {}, "Um Dockerfile é necessário ou confira se o branch e a subpasta estão certos.")));
    return;
  }
  const v = valoresDaAnalise(an);
  const { form } = formularioSolucao(v, {
    modo: "criar",
    aoSalvar: async (dados) => { const r = await api("/api/assistente/criar", { method: "POST", body: { dados } }); pintarCriada(alvo, r, dados); },
  });
  const temErro = an.avisos.some((a) => a.nivel === "erro");
  alvo.replaceChildren(cabecalho, h("section", { class: "painel" }, h("h2", {}, "3. Revise e crie"),
    temErro ? h("p", { class: "texto-erro topo-ajuste" }, "Existem problemas para resolver. É possível criar e testar, mas não é recomendado usar com dados reais antes das correções.") : null,
    form));
}

async function telaEditarSolucao(id) {
  const v = await api(`/api/assistente/solucoes/${encodeURIComponent(id)}`);
  document.title = `Editar ${v.nome} · Portal de Soluções LOG20`;
  const reanalise = h("div", { id: "ns-reanalise" });
  const { form, aplicarAnalise } = formularioSolucao(v, {
    modo: "editar",
    aoSalvar: async (dados) => {
      const r = await api(`/api/assistente/solucoes/${encodeURIComponent(id)}`, { method: "PUT", body: { dados } });
      location.hash = `#/admin/solucao/${id}`;
      setTimeout(() => toast(`Alterações salvas. ${r.aviso}`), 300);
    },
  });
  const botaoReanalisar = h("button", {
    type: "button", class: "botao", onclick: async (ev) => {
      const b = ev.currentTarget;
      b.disabled = true;
      b.replaceChildren(h("span", { class: "giro", "aria-hidden": "true" }), "Analisando o código mais recente…");
      try {
        const an = await api("/api/assistente/analisar", { method: "POST", body: { repositorio: v.repositorio, ref: v.ref, subpasta: v.subpasta } });
        pintarReanalise(reanalise, an, aplicarAnalise(an));
      } catch (e) { toast(e.message, "erro"); }
      b.disabled = false;
      b.textContent = "Analisar novamente";
    },
  }, "Analisar novamente");
  const repo = v.repositorio.replace(/^https:\/\//, "").replace(/\.git$/, "");
  $conteudo.replaceChildren(
    h("a", { class: "voltar", href: `#/admin/solucao/${id}` }, v.nome),
    h("section", { class: "intro intro-linha" },
      h("div", {}, h("h1", {}, `Editar ${v.nome}`),
        h("p", {}, `${repo} · ${v.ref}${v.subpasta ? ` · ${v.subpasta}/` : ""}. As mudanças serão aplicadas na próxima vez que Reiniciar (ou Atualizar, se o Dockerfile mudar) a solução.`)),
      h("div", { class: "intro-lado" }, botaoReanalisar)),
    reanalise,
    h("section", { class: "painel" }, h("h2", {}, "Dados da solução"), form));
}

function pintarReanalise(alvo, an, ap) {
  const itens = [];
  if (ap.novas.length) {
    const marcas = ap.novas.map((c) => ({ c, caixa: h("input", { type: "checkbox", checked: true, "aria-label": `Adicionar ${c.nome}` }) }));
    itens.push(h("div", { class: "bloco" },
      h("h3", {}, plural(ap.novas.length, "configuração nova no código", "configurações novas no código")),
      h("ul", { class: "lista-novas" }, marcas.map(({ c, caixa }) => h("li", {}, h("label", { class: "campo-check" }, caixa,
        h("span", {}, h("code", {}, c.nome), h("small", {}, ` ${c.tipo}${c.obrigatoria ? ", obrigatória" : ""}. ${c.ajuda || ""}`)))))),
      h("button", { type: "button", class: "botao pequeno", onclick: (ev) => {
        let n = 0;
        for (const { c, caixa } of marcas) if (caixa.checked) { ap.adicionar(c); n += 1; }
        ev.currentTarget.disabled = true;
        toast(`${plural(n, "configuração adicionada", "configurações adicionadas")} à tabela. Salve para aplicar.`);
      } }, "Adicionar as marcadas")));
  }
  if (ap.siteEstatico) {
    itens.push(h("div", { class: "bloco" },
      h("h3", {}, "É um site estático. As variáveis precisam entrar na construção"),
      h("p", { class: "nota" }, "Para sites Vite, React ou Vue, nada lê variável quando o site está no ar, então as variáveis fixas e todas as configurações vão para o build (momento da criação). Marque para o portal passar as variáveis fixas e as configurações para o build."),
      h("button", { type: "button", class: "botao pequeno", onclick: (ev) => { ap.marcarSiteEstatico(); ev.currentTarget.disabled = true; toast("Marcado. Salve e depois clique em Atualizar."); } }, "Marcar site estático")));
  }
  if (ap.paraConstrucao.length) {
    itens.push(h("div", { class: "bloco" },
      h("h3", {}, "Configurações lidas na construção"),
      h("p", { class: "nota" }, `${ap.paraConstrucao.join(", ")}: o código lê na hora de criar a solução, então o valor só chega ao sistema pelo build.`),
      h("button", { type: "button", class: "botao pequeno", onclick: (ev) => { ap.marcarConstrucao(); ev.currentTarget.disabled = true; toast("Marcadas. Salve e depois clique em Atualizar."); } }, "Marcar para a construção")));
  }
  if (ap.dockerNovo) {
    itens.push(h("div", { class: "bloco" },
      h("h3", {}, "O Dockerfile sugerido mudou"),
      h("p", { class: "nota" }, "O código mudou desde a criação (ex.: dependências ou tecnologia). Você pode trocar o Dockerfile guardado pelo sugerido agora. O atual se perde se você salvar."),
      h("details", {}, h("summary", {}, "Ver o sugerido"), h("pre", { class: "saida curta" }, an.dockerfile)),
      h("button", { type: "button", class: "botao pequeno topo-8", onclick: (ev) => { ap.usarDockerfile(); ev.currentTarget.disabled = true; toast("Dockerfile alterado no formulário. Salve para valer."); } }, "Usar o Dockerfile sugerido")));
  }
  alvo.replaceChildren(
    painelDeteccao(an, "Análise do código mais recente"),
    h("section", { class: "painel" }, h("h2", {}, "O que mudou"),
      itens.length ? itens : h("p", { class: "texto-ok" }, "Nada novo: as configurações e o Dockerfile já estão em dia com o código.")));
  alvo.scrollIntoView({ behavior: "smooth", block: "start" });
}

function pintarCriada(alvo, r, dados) {
  alvo.replaceChildren(h("section", { class: "painel criada" },
    h("h2", {}, `${dados.nome} está no catálogo`),
    h("p", {}, `Criada neste servidor, na porta ${dados.porta_host}. O próximo passo é instalar. O portal baixa o código, constrói a imagem e sobe o sistema.`),
    h("div", { class: "linha-botoes" },
      h("button", {
        type: "button", class: "botao primario", onclick: async (ev) => {
          ev.currentTarget.disabled = true;
          try {
            await api(`/api/solucoes/${r.id}/acoes/iniciar`, { method: "POST" });
            location.hash = `#/admin/solucao/${r.id}`;
          } catch (e) { toast(e.message, "erro"); ev.currentTarget.disabled = false; }
        },
      }, "Instalar e iniciar"),
      h("a", { class: "botao", href: `#/admin/solucao/${r.id}` }, "Ver a solução"),
      h("button", { type: "button", class: "botao sutil", onclick: () => baixarArquivosLocais(r.id) }, "Baixar arquivos (.zip)")),
    h("p", { class: "nota" }, `Configurações obrigatórias precisam ser preenchidas em Gerenciar → Configurações antes de iniciar. Para usar em outros servidores, descompacte o .zip na raiz do repositório do portal (cria solucoes/${r.id}/). Depois, libere o sistema em Acessos.`)));
  alvo.scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ================= admin: backups ================= */

const estadoBackups = { aba: "lista", dados: null, logAberto: false, filtro: "" };
const MOTIVOS_BACKUP = {
  agendado: "Automático", manual: "Manual", solucao: "Só de uma solução", "antes-de-atualizar": "Antes de atualizar o portal",
  "antes-de-restaurar": "Segurança, antes de restaurar",
  "antes-de-importar": "Segurança, antes de importar",
};

function tamanho(bytes) {
  let n = Number(bytes) || 0;
  for (const u of ["B", "KB", "MB", "GB"]) {
    if (n < 1024 || u === "GB") return u === "B" ? `${n} B` : `${n.toLocaleString("pt-BR", { maximumFractionDigits: 1 })} ${u}`;
    n /= 1024;
  }
  return "";
}
function dataIso(iso, curto) { return iso ? quando(Date.parse(iso) / 1000, curto) : "Nunca"; }

function rotuloItem(it, nomes) {
  const sol = it.solucao ? nomes.get(it.solucao) || it.solucao : null;
  if (it.tipo === "portal") return it.arquivo.endsWith("portal.db") ? "Portal: usuários, acessos e histórico" : "Portal: configurações e segredos";
  if (it.tipo === "contas") return "Contas do banco do servidor";
  if (it.tipo === "banco") return `Banco ${it.banco}${sol ? ` (${sol})` : " (sem solução: criado à mão)"}`;
  if (it.tipo === "volume") return `Arquivos de ${sol}: ${it.chave}`;
  if (it.tipo === "externo") return `Banco externo de ${sol} (${it.servidor})`;
  if (it.tipo === "supabase-banco") return `Supabase de ${sol}: banco (cópia para conferência)`;
  if (it.tipo === "supabase-segredos") return `Supabase de ${sol}: chaves`;
  if (it.tipo === "supabase-volume") return `Supabase de ${sol}: ${({ dados: "banco e usuários do login", config: "configuração do banco", arquivos: "arquivos enviados", snippets: "consultas salvas do Studio" })[it.chave] || it.chave}`;
  return it.arquivo;
}

async function telaBackups() {
  document.title = "Backups · Portal de Soluções LOG20";
  estadoBackups.dados = await api("/api/backups");
  const abas = [["lista", "Backups"], ["importar", "Importar arquivo"], ["ajustes", "Ajustes"]];
  $conteudo.replaceChildren(
    h("section", { class: "intro intro-linha" },
      h("div", {}, h("h1", {}, "Backups"),
        h("p", {}, "Cópias dos bancos das soluções, das pastas de dados e dos dados do portal.")),
      h("div", { class: "intro-lado" },
        h("button", { type: "button", class: "botao", id: "backup-solucao", onclick: dialogoBackupSolucao }, "Backup de uma solução"),
        h("button", { type: "button", class: "botao primario", id: "backup-agora", onclick: fazerBackupAgora }, "Fazer backup agora"))),
    h("div", { id: "backup-alerta" }),
    h("div", { class: "grade-backup", id: "backup-resumo" }),
    h("div", { id: "backup-tarefa" }),
    h("div", { class: "abas", role: "tablist" }, abas.map(([k, rotulo]) =>
      h("button", { type: "button", role: "tab", "aria-selected": String(estadoBackups.aba === k),
        onclick: () => { estadoBackups.aba = k; telaBackups(); } }, rotulo))),
    h("div", { id: "backup-corpo" }));
  pintarResumoBackup();
  if (estadoBackups.aba === "ajustes") pintarAjustesBackup();
  else if (estadoBackups.aba === "importar") pintarImportar();
  else pintarListaBackups();
}

async function atualizarBackups() {
  const antes = estadoBackups.dados;
  estadoBackups.dados = await api("/api/backups");
  pintarResumoBackup();
  const mudouLista = JSON.stringify(antes?.backups) !== JSON.stringify(estadoBackups.dados.backups);
  if (estadoBackups.aba === "lista" && mudouLista) pintarListaBackups();
  const mudouImport = JSON.stringify(antes?.importacoes) !== JSON.stringify(estadoBackups.dados.importacoes);
  if (estadoBackups.aba === "importar" && mudouImport && !document.querySelector("#backup-corpo :focus")) pintarImportacoes();
}

function pintarResumoBackup() {
  const d = estadoBackups.dados;
  const e = d.estado || {};
  const t = d.tarefa;
  emAndamento = t?.estado === "executando";
  for (const id of ["backup-agora", "backup-solucao"]) {
    const botao = document.getElementById(id);
    if (botao) botao.disabled = emAndamento;
  }

  // Alerta: último falhou, ou nenhum backup bom nas últimas 48 h.
  const alerta = document.getElementById("backup-alerta");
  const ultimoBom = e.ultimo_sucesso_em ? Date.parse(e.ultimo_sucesso_em) : 0;
  const msgs = [];
  if (e.ultimo && !e.ultimo_ok) msgs.push(h("li", {}, `O último backup (${dataIso(e.ultimo_em)}) teve erros: ${(e.ultimo_erros || []).join("; ")}`));
  if (e.ultimo_erro && (!e.ultimo_em || Date.parse(e.ultimo_erro_em) > Date.parse(e.ultimo_em))) msgs.push(h("li", {}, e.ultimo_erro));
  if (d.ajustes.ativo && (!ultimoBom || Date.now() - ultimoBom > 48 * 3600e3)) msgs.push(h("li", {}, ultimoBom ? `Nenhum backup completo desde ${dataIso(e.ultimo_sucesso_em)}.` : "Ainda não há nenhum backup completo deste servidor."));
  if (!d.ajustes.ativo) msgs.push(h("li", {}, "O backup automático está desligado (Ajustes)."));
  if (!d.disco.ok) msgs.push(h("li", {}, `A pasta de backup não pode ser usada: ${d.disco.erro}`));
  alerta.replaceChildren(msgs.length ? h("div", { class: "aviso" }, h("strong", {}, "Atenção"), h("ul", {}, msgs)) : null);

  const cartao = (rotulo, valor, detalhe, extra, caminho) => h("div", { class: "cartao-backup" },
    h("span", { class: "deteccao-rotulo" }, rotulo), h("strong", {}, valor), detalhe ? h("small", {}, detalhe) : null,
    caminho ? h("small", { class: "caminho", title: caminho }, caminho) : null, extra || null);
  const ultimo = d.backups.find((b) => !b.parcial);
  const copia = d.copia
    ? cartao("Segunda cópia", d.ajustes.copiar ? "Ligada" : "Desligada", "Pasta:", null, d.copia)
    : cartao("Segunda cópia", "Não configurada", "Uma cópia em outro disco ou na rede protege contra perda de dados. Veja Ajustes.");
  document.getElementById("backup-resumo").replaceChildren(
    cartao("Último backup", ultimo ? dataIso(ultimo.criado_em) : "Nenhum",
      ultimo ? `${MOTIVOS_BACKUP[ultimo.motivo] || ultimo.motivo} · ${tamanho(ultimo.bytes)}` : "Clique em Fazer backup agora.",
      ultimo ? pilula(ultimo.ok ? "rodando" : "com_problema", ultimo.ok ? "Completo" : "Com erros") : null),
    cartao("Próximo automático", d.proximo ? dataIso(d.proximo) : "Desligado",
      d.ajustes.ativo ? `Todo dia às ${d.ajustes.horarios.join(" e ")}` : "Ligue em Ajustes."),
    cartao("Espaço livre", d.disco.ok ? d.disco.livre_texto : "—", `de ${d.disco.total_texto}, na pasta:`, null, d.pasta),
    copia);

  const alvo = document.getElementById("backup-tarefa");
  if (!t) { alvo.replaceChildren(); return; }
  const rotuloEstado = { executando: ["ocupado", t.etapa || "Em andamento"], concluida: ["rodando", "Concluída"], falhou: ["com_problema", "Falhou"] }[t.estado];
  let pre = alvo.querySelector("pre");
  if (!pre) {
    pre = h("pre", { class: "saida curta", tabindex: "0" });
    alvo.replaceChildren(h("section", { class: "painel tarefa-backup" },
      h("div", { class: "linha-titulo" }, h("h2", {}, ""), h("span", { class: "tarefa-estado" })),
      h("details", { open: emAndamento || estadoBackups.logAberto, ontoggle: (ev) => { estadoBackups.logAberto = ev.target.open; } },
        h("summary", {}, "Ver o registro"), pre),
      h("p", { class: "texto-erro", hidden: true })));
  }
  const painel = alvo.querySelector(".painel");
  painel.querySelector("h2").textContent = `Operação: ${t.acao}`;
  painel.querySelector(".tarefa-estado").replaceChildren(pilula(rotuloEstado[0], rotuloEstado[1]));
  const erro = painel.querySelector(".texto-erro");
  erro.hidden = !t.erro; erro.textContent = t.erro || "";
  const noFim = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  if (pre.textContent !== t.log) { pre.textContent = t.log; if (noFim) pre.scrollTop = pre.scrollHeight; }
}

async function fazerBackupAgora() {
  try {
    await api("/api/backups", { method: "POST" });
    estadoBackups.logAberto = true;
    toast("Backup iniciado.");
    await atualizarBackups();
  } catch (e) { toast(e.message, "erro"); }
}

/** Soluções que podem ter backup isolado (dentro do backup pelo manifesto e pelos Ajustes). */
function solucoesComBackup() {
  const d = estadoBackups.dados;
  const fora = new Set(d.ajustes.excluir || []);
  // Só as que têm algo para guardar (banco, pastas de dados, banco externo ou Supabase).
  return d.solucoes.filter((s) => s.manifesto_inclui && !fora.has(s.id)
    && (s.banco || s.volumes.length || s.externo || s.supabase));
}

function dialogoBackupSolucao() {
  const lista = solucoesComBackup();
  const sel = h("select", { id: "bs-sol" }, lista.map((s) => h("option", { value: s.id }, s.nome)));
  if (estadoBackups.filtro && lista.some((s) => s.id === estadoBackups.filtro)) sel.value = estadoBackups.filtro;
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const botao = h("button", { type: "button", class: "botao primario", disabled: !lista.length, onclick: async () => {
    botao.disabled = true;
    try {
      await api("/api/backups", { method: "POST", body: { solucao: sel.value } });
      dlg.close();
      estadoBackups.logAberto = true;
      toast("Backup da solução iniciado.");
      await atualizarBackups();
    } catch (e) { erro.textContent = e.message; erro.hidden = false; botao.disabled = false; }
  } }, "Fazer backup");
  const dlg = dialogo({
    titulo: "Backup de uma solução",
    descricao: "Guarda só os dados desta solução: banco, pastas de dados, banco externo e Supabase. Não inclui o portal (usuários e acessos) nem as outras soluções.",
    corpo: lista.length
      ? [h("div", { class: "campo" }, h("label", { for: "bs-sol" }, "Solução"), sel), erro]
      : h("p", { class: "nota" }, "Nenhuma solução está no backup (veja Ajustes)."),
    rodape: [h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"), botao],
  });
}

function dialogoApagarTodos(alvos, nomes) {
  const sid = estadoBackups.filtro;
  const fixados = alvos.filter((b) => b.fixado).length;
  const apagar = alvos.length - fixados;
  const conf = h("input", { type: "text", id: "ap-conf", autocomplete: "off", spellcheck: false });
  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const botao = h("button", { type: "button", class: "botao perigo-cheio", disabled: true, onclick: async () => {
    botao.disabled = true;
    try {
      const r = await api("/api/backups/apagar", { method: "POST", body: { confirmacao: conf.value, solucao: sid } });
      dlg.close();
      const falhas = r.erros?.length ? `; ${plural(r.erros.length, "não pôde ser apagado", "não puderam ser apagados")}: ${r.erros.join("; ")}` : "";
      toast(`${plural(r.apagados, "backup apagado", "backups apagados")}${r.fixados ? `; ${plural(r.fixados, "fixado mantido", "fixados mantidos")}` : ""}${falhas}.`, falhas ? "erro" : undefined);
      estadoBackups.dados = null;
      await atualizarBackups();
      pintarListaBackups();
    } catch (e) { erro.textContent = e.message; erro.hidden = false; botao.disabled = false; }
  } }, `Apagar ${plural(apagar, "backup", "backups")}`);
  conf.addEventListener("input", () => { botao.disabled = conf.value.trim().toUpperCase() !== "APAGAR" || !apagar; });
  const d = estadoBackups.dados;
  const dlg = dialogo({
    titulo: sid ? `Apagar os backups só de ${nomes.get(sid) || sid}?` : "Apagar todos os backups?",
    corpo: [
      h("p", { class: "dialogo-texto" }, sid
        ? `Apaga ${plural(apagar, "backup feito só desta solução", "backups feitos só desta solução")}. Os backups completos têm todas as soluções e continuam na lista.`
        : `Apaga ${plural(apagar, "backup", "backups")} desta pasta.` + (alvos.some((b) => !b.parcial && !b.fixado)
          ? " Inclui os backups completos: até o próximo backup, não haverá como voltar os dados." : "")),
      fixados ? h("p", { class: "dialogo-texto" }, `${plural(fixados, "backup fixado fica", "backups fixados ficam")}. Para apagar, desfixe antes.`) : null,
      d.copia ? h("p", { class: "dialogo-texto" }, "A segunda cópia (pasta de rede) não é apagada.") : null,
      h("p", { class: "dialogo-texto" }, "Não é possível desfazer."),
      h("div", { class: "campo" }, h("label", { for: "ap-conf" }, "Para confirmar, digite APAGAR"), conf),
      erro,
    ],
    rodape: [h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"), botao],
  });
}

function pintarListaBackups() {
  const d = estadoBackups.dados;
  const alvo = document.getElementById("backup-corpo");
  const nomes = new Map(d.solucoes.map((s) => [s.id, s.nome]));
  // Filtro por solução: os backups com dados dela (completos e os feitos só dela).
  const temDados = (b, sid) => b.itens.some((i) => i.solucao === sid) || (b.parcial && (b.solucoes || []).includes(sid));
  const comBackup = d.solucoes.filter((s) => d.backups.some((b) => temDados(b, s.id)));
  if (estadoBackups.filtro && !comBackup.some((s) => s.id === estadoBackups.filtro)) estadoBackups.filtro = "";
  const sid = estadoBackups.filtro;
  const visiveis = sid ? d.backups.filter((b) => temDados(b, sid)) : d.backups;
  const alvosApagar = sid ? d.backups.filter((b) => b.parcial && (b.solucoes || []).length === 1 && b.solucoes[0] === sid) : d.backups;
  const barra = d.backups.length ? h("div", { class: "barra-ferramentas barra-backups" },
    h("label", { class: "filtro-rotulo" }, "Mostrar",
      h("select", { class: "filtro-select", onchange: (ev) => { estadoBackups.filtro = ev.target.value; pintarListaBackups(); } },
        h("option", { value: "" }, "Todas as soluções"),
        comBackup.map((s) => h("option", { value: s.id, selected: s.id === sid }, s.nome)))),
    h("span", { class: "espaco" }),
    alvosApagar.some((b) => !b.fixado) ? h("button", { type: "button", class: "botao pequeno perigo",
      onclick: () => dialogoApagarTodos(alvosApagar, nomes) }, sid ? "Apagar os backups só desta solução" : "Apagar todos") : null) : null;
  if (!d.backups.length) {
    alvo.replaceChildren(h("div", { class: "vazio" }, h("strong", {}, "Nenhum backup ainda"),
      h("p", {}, d.ajustes.ativo ? `O primeiro automático sai ${dataIso(d.proximo).toLowerCase()}. Ou clique em Fazer backup agora.` : "Clique em Fazer backup agora.")));
    return;
  }
  const linhas = visiveis.map((b) => {
    const verif = b.verificacao;
    const situacao = !b.ok ? pilula("com_problema", "Com erros")
      : verif ? pilula(verif.ok ? "rodando" : "com_problema", verif.ok ? "Conferido" : "Problema na conferência")
      : pilula("instalado", "Completo");
    const restauraveis = [...new Set(b.itens.filter((i) => i.solucao && (i.tipo === "banco" || i.tipo === "volume" || i.tipo === "supabase-volume")).map((i) => i.solucao))]
      .filter((sid) => nomes.has(sid));
    return h("tr", { class: b.ok ? "" : "linha-erro" },
      h("td", {}, h("strong", {}, dataIso(b.criado_em)), h("div", { class: "sub" }, MOTIVOS_BACKUP[b.motivo] || b.motivo,
        b.parcial && (b.solucoes || []).length ? `: ${b.solucoes.map((x) => nomes.get(x) || x).join(", ")}` : "",
        b.fixado ? h("span", { class: "etiqueta" }, "Fixado") : null)),
      h("td", {}, h("details", { class: "itens-backup" },
        h("summary", {}, `${plural(b.itens.length, "arquivo", "arquivos")} · ${tamanho(b.bytes)}`),
        h("ul", {}, b.itens.map((i) => h("li", {}, rotuloItem(i, nomes),
          h("small", {}, ` ${tamanho(i.bytes)}${i.tabelas != null ? ` · ${plural(i.tabelas, "tabela", "tabelas")}` : ""}${i.arquivos != null ? ` · ${plural(i.arquivos, "arquivo", "arquivos")}` : ""}`)))),
        b.erros.length ? h("ul", { class: "erros-backup" }, b.erros.map((e) => h("li", {}, e))) : null,
        verif && !verif.ok ? h("ul", { class: "erros-backup" }, verif.problemas.map((e) => h("li", {}, e))) : null)),
      h("td", {}, situacao, verif ? h("div", { class: "sub" }, `Conferido ${dataIso(verif.em, true).toLowerCase()}`) : null),
      h("td", { class: "acoes-backup" },
        restauraveis.length ? h("button", { type: "button", class: "botao pequeno", onclick: () => dialogoRestaurar(b, restauraveis, nomes) }, "Restaurar…") : null,
        h("button", { type: "button", class: "botao pequeno sutil", title: "Restaura cada banco num banco de teste e confere os arquivos, sem alterar o que está em uso",
          onclick: () => acaoBackup(`/api/backups/${b.nome}/verificar`, "POST", null, "Conferência iniciada.") }, "Conferir"),
        h("button", { type: "button", class: "botao pequeno sutil", title: b.fixado ? "Volta a seguir a limpeza automática" : "Nunca é apagado pela limpeza automática",
          onclick: () => acaoBackup(`/api/backups/${b.nome}/fixar`, "PUT", { fixado: !b.fixado }, b.fixado ? "Backup desfixado." : "Backup fixado. Não será apagado.") }, b.fixado ? "Desfixar" : "Fixar"),
        b.fixado ? null : h("button", { type: "button", class: "link-limpar", onclick: async () => {
          if (!(await confirmar({ titulo: "Apagar este backup?", texto: `${dataIso(b.criado_em)} (${tamanho(b.bytes)}). Não é possível desfazer.`, botao: "Apagar backup", perigo: true }))) return;
          acaoBackup(`/api/backups/${b.nome}`, "DELETE", null, "Backup apagado.");
        } }, "Apagar")));
  });
  const aj = d.ajustes;
  alvo.replaceChildren(
    barra,
    h("div", { class: "tabela-rolagem" }, h("table", { class: "tabela-simples tabela-backups" },
      h("thead", {}, h("tr", {}, ["Quando", "Conteúdo", "Situação", ""].map((x) => h("th", {}, x)))),
      h("tbody", {}, linhas))),
    h("p", { class: "nota" }, `Guardados: os automáticos dos últimos ${plural(aj.manter_diarios, "dia", "dias")}, um por semana nas últimas ${aj.manter_semanais}, um por mês nos últimos ${aj.manter_mensais} os ${aj.manter_avulsos} manuais completos mais recentes e os ${aj.manter_avulsos} mais recentes de cada solução. Os fixados nunca são apagados pela limpeza automática.`));
}

async function acaoBackup(url, method, body, ok) {
  try {
    await api(url, { method, body: body || undefined });
    toast(ok);
    if (method !== "POST") estadoBackups.dados = null;
    await atualizarBackups();
    if (method !== "POST") pintarListaBackups();
  } catch (e) { toast(e.message, "erro"); }
}

function dialogoRestaurar(b, restauraveis, nomes) {
  const sel = h("select", { id: "rest-sol" }, restauraveis.map((sid) => h("option", { value: sid }, nomes.get(sid))));
  const partes = h("div", { class: "rest-partes" });
  const confirmacao = h("input", { type: "text", id: "rest-conf", autocomplete: "off", spellcheck: false });
  const rotuloConf = h("label", { for: "rest-conf" });
  const botao = h("button", { type: "submit", class: "botao perigo-cheio", form: "form-restaurar", disabled: true }, "Restaurar");
  const pintar = () => {
    const sid = sel.value;
    const itens = b.itens.filter((i) => i.solucao === sid);
    const banco = itens.find((i) => i.tipo === "banco");
    const vols = itens.filter((i) => i.tipo === "volume");
    const sbase = itens.filter((i) => i.tipo === "supabase-volume" || i.tipo === "supabase-segredos");
    partes.replaceChildren(
      banco ? h("label", { class: "campo-check" }, h("input", { type: "checkbox", name: "banco", checked: true }),
        h("span", {}, h("strong", {}, "Banco de dados"), h("small", {}, ` ${banco.banco}${banco.tabelas != null ? `, ${plural(banco.tabelas, "tabela", "tabelas")}` : ""}, ${tamanho(banco.bytes)}`))) : null,
      vols.length ? h("label", { class: "campo-check" }, h("input", { type: "checkbox", name: "arquivos", checked: true }),
        h("span", {}, h("strong", {}, "Arquivos"), h("small", {}, ` ${vols.map((v) => v.chave).join(", ")}, ${tamanho(vols.reduce((s, v) => s + v.bytes, 0))}`))) : null,
      sbase.length ? h("label", { class: "campo-check" }, h("input", { type: "checkbox", name: "supabase", checked: true }),
        h("span", {}, h("strong", {}, "Supabase do sistema"), h("small", {}, ` banco, usuários do login, arquivos e chaves, juntos; ${tamanho(sbase.reduce((s, v) => s + v.bytes, 0))}`))) : null);
    rotuloConf.textContent = `Para confirmar, digite ${sid}`;
    confirmacao.value = "";
    botao.disabled = true;
  };
  sel.addEventListener("change", pintar);
  confirmacao.addEventListener("input", () => { botao.disabled = confirmacao.value.trim() !== sel.value; });
  pintar();
  const form = h("form", { id: "form-restaurar", class: "config" },
    h("div", { class: "campo" }, h("label", { for: "rest-sol" }, "Solução"), sel),
    h("div", { class: "campo" }, h("label", {}, "O que voltar"), partes),
    h("ul", { class: "lista-explica" },
      h("li", {}, "Antes, o portal faz um backup de segurança desta solução."),
      h("li", {}, "A solução é parada. Se estava no ar, volta automaticamente."),
      h("li", {}, "O banco é restaurado numa cópia e só substitui o atual se tudo der certo."),
      h("li", {}, h("strong", {}, "Tudo o que foi gravado depois deste backup se perde."))),
    h("div", { class: "campo" }, rotuloConf, confirmacao));
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const banco = !!form.querySelector("[name=banco]")?.checked;
    const arquivos = !!form.querySelector("[name=arquivos]")?.checked;
    const supabase = !!form.querySelector("[name=supabase]")?.checked;
    if (!banco && !arquivos && !supabase) { toast("Marque o que deve voltar.", "erro"); return; }
    botao.disabled = true;
    try {
      await api(`/api/backups/${b.nome}/restaurar`, { method: "POST", body: { solucao: sel.value, banco, arquivos, supabase, confirmacao: confirmacao.value.trim() } });
      dlg.close();
      estadoBackups.logAberto = true;
      toast("Restauração iniciada. Acompanhe o registro.");
      await atualizarBackups();
    } catch (e) { toast(e.message, "erro"); botao.disabled = false; }
  });
  const dlg = dialogo({
    titulo: "Restaurar dados de uma solução",
    descricao: `Backup de ${dataIso(b.criado_em)}`,
    corpo: form,
    rodape: [h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"), botao],
  });
}

/* ---------- importar arquivo ---------- */

function enviarArquivo(arquivo, aoProgresso) {
  // XMLHttpRequest para mostrar o progresso; o arquivo vai cru no corpo (o servidor grava direto no disco).
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/backups/importacoes?nome=${encodeURIComponent(arquivo.name)}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) aoProgresso(ev.loaded / ev.total); };
    xhr.onload = () => {
      let dados = {};
      try { dados = JSON.parse(xhr.responseText || "{}"); } catch { /* resposta sem JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(dados);
      else reject(new Error(dados.detail || `Erro ${xhr.status} ao enviar o arquivo.`));
    };
    xhr.onerror = () => reject(new Error("A conexão caiu durante o envio."));
    xhr.send(arquivo);
  });
}

function pintarImportar() {
  const d = estadoBackups.dados;
  const alvo = document.getElementById("backup-corpo");
  const entrada = h("input", { type: "file", id: "imp-arquivo", class: "so-leitor", accept: ".sql,.gz,.dump,.backup,.zip,.tar,.tgz" });
  const barra = h("div", { class: "progresso", hidden: true }, h("span", {}));
  const estado = h("p", { class: "nota", "aria-live": "polite" });
  const zona = h("label", { class: "zona-arquivo", for: "imp-arquivo" },
    h("strong", {}, "Escolha um arquivo ou arraste para cá"),
    h("span", {}, `.sql, .sql.gz ou .dump para um banco; .zip ou .tar.gz para uma pasta de dados. Até ${String(d.importacao_max_gb).replace(".", ",")} GB.`));
  const enviar = async (arquivo) => {
    if (!arquivo) return;
    zona.classList.add("ocupada");
    barra.hidden = false;
    barra.firstChild.style.width = "0%";
    estado.textContent = `Enviando ${arquivo.name} (${tamanho(arquivo.size)})…`;
    try {
      const info = await enviarArquivo(arquivo, (f) => {
        barra.firstChild.style.width = `${Math.round(f * 100)}%`;
        if (f >= 1) estado.textContent = "Recebido. Analisando o arquivo…";
      });
      estado.textContent = "";
      toast(`${info.arquivo}: pronto para importar.`);
      estadoBackups.dados = await api("/api/backups");
      pintarImportacoes(info.id);
    } catch (e) {
      estado.textContent = "";
      toast(e.message, "erro");
    }
    barra.hidden = true;
    zona.classList.remove("ocupada");
    entrada.value = "";
  };
  entrada.addEventListener("change", () => enviar(entrada.files[0]));
  for (const ev of ["dragenter", "dragover"]) zona.addEventListener(ev, (e) => { e.preventDefault(); zona.classList.add("arrastando"); });
  for (const ev of ["dragleave", "drop"]) zona.addEventListener(ev, () => zona.classList.remove("arrastando"));
  zona.addEventListener("drop", (e) => { e.preventDefault(); enviar(e.dataTransfer.files[0]); });

  alvo.replaceChildren(
    h("section", { class: "painel" },
      h("h2", {}, "Importar dados para uma solução"),
      h("p", { class: "nota sem-topo" }, "Por exemplo, o banco da máquina de quem desenvolveu o sistema, ou as planilhas que ele já tinha. Primeiro o arquivo é enviado e conferido, alterações só são efetivadas após a confirmação."),
      entrada, zona, barra, estado,
      h("details", { class: "como-gerar" },
        h("summary", {}, "Como gerar o arquivo do banco"),
        h("p", {}, "Na máquina onde o banco está hoje (PowerShell ou terminal):"),
        h("pre", { class: "saida curta" }, "pg_dump -h localhost -U <usuario> -d <banco> --no-owner --no-privileges -f banco.sql"),
        h("p", { class: "nota" }, `Use o SQL puro (.sql). Ele funciona mesmo se o PostgreSQL da máquina de quem desenvolveu for mais novo que o do servidor${d.importacao_versao ? ` (${d.importacao_versao})` : ""}. O .dump (pg_dump -Fc) só abre se a versão de lá for igual ou mais antiga. Se esquecer o --no-owner, o portal ignora as linhas de dono e permissão.`))),
    h("div", { id: "lista-importacoes" }));
  pintarImportacoes();
}

function pintarImportacoes(destacar) {
  const d = estadoBackups.dados;
  const alvo = document.getElementById("lista-importacoes");
  if (!alvo) return;
  if (!d.importacoes.length) { alvo.replaceChildren(); return; }
  alvo.replaceChildren(h("h2", { class: "titulo-secao" }, "Arquivos recebidos"),
    ...d.importacoes.map((imp) => cartaoImportacao(imp, imp.id === destacar)));
}

function cartaoImportacao(imp, destacado) {
  const d = estadoBackups.dados;
  const banco = imp.tipo === "banco";
  const opcoes = d.solucoes.filter((s) => (banco ? s.banco : s.volumes.length));
  const detalhe = banco
    ? [imp.formato === "sql" ? "Banco de dados (SQL)" : "Banco de dados (.dump)", plural(imp.tabelas, "tabela", "tabelas"),
       imp.linhas_dados != null ? `${imp.linhas_dados.toLocaleString("pt-BR")} linhas de dados` : null,
       imp.versao_origem ? `PostgreSQL ${imp.versao_origem} na origem` : null].filter(Boolean).join(" · ")
    : `Arquivos · ${plural(imp.arquivos, "arquivo", "arquivos")} · ${tamanho(imp.bytes_conteudo)}`;
  const descartar = h("button", { type: "button", class: "link-limpar", onclick: async () => {
    try { await api(`/api/backups/importacoes/${imp.id}`, { method: "DELETE" }); toast("Arquivo descartado."); await atualizarBackups(); pintarImportacoes(); }
    catch (e) { toast(e.message, "erro"); }
  } }, "Descartar");
  if (!opcoes.length) {
    return h("section", { class: "painel importacao" },
      h("div", { class: "linha-titulo" }, h("h3", {}, imp.arquivo), descartar),
      h("p", { class: "nota" }, detalhe),
      h("p", { class: "texto-erro" }, banco ? "Nenhuma solução usa o banco do servidor." : "Nenhuma solução tem pasta de dados."));
  }
  const sel = h("select", { name: "solucao" }, opcoes.map((s) => h("option", { value: s.id }, s.nome)));
  const pasta = h("select", { name: "chave" });
  const campoPasta = h("div", { class: "campo", hidden: banco }, h("label", {}, "Pasta de dados"), pasta,
    h("label", { class: "campo-check topo-8" }, h("input", { type: "checkbox", name: "juntar" }),
      h("span", {}, h("strong", {}, "Manter os arquivos que já estão na pasta"), h("small", {}, " Sem marcar, a pasta fica só com o conteúdo do arquivo enviado."))));
  const conf = h("input", { type: "text", name: "confirmacao", autocomplete: "off", spellcheck: false });
  const rotuloConf = h("label", {});
  const botao = h("button", { type: "submit", class: "botao perigo-cheio", disabled: true }, "Importar");
  const atualizarSel = () => {
    const s = opcoes.find((o) => o.id === sel.value);
    pasta.replaceChildren(...(s?.volumes || []).map((v) => h("option", { value: v }, v)));
    rotuloConf.textContent = `Para confirmar, digite ${sel.value}`;
    conf.value = "";
    botao.disabled = true;
  };
  sel.addEventListener("change", atualizarSel);
  conf.addEventListener("input", () => { botao.disabled = conf.value.trim() !== sel.value; });
  atualizarSel();
  const form = h("form", { class: "config" },
    imp.avisos.length ? h("ul", { class: "avisos-lista" }, imp.avisos.map((a) => h("li", { class: "aviso-item atencao" }, h("p", {}, a)))) : null,
    h("div", { class: "form-grade" }, h("div", { class: "campo" }, h("label", {}, "Solução"), sel), campoPasta),
    h("p", { class: "nota" }, banco
      ? "O banco atual da solução é substituído pelo do arquivo. Antes, o portal faz um backup de segurança. A importação vai primeiro para um banco temporário e só ocorre a substituição se tudo der certo. A solução é parada e, se estava no ar, volta automaticamente."
      : "Antes, o portal faz um backup de segurança da solução. A solução é parada e, se estava no ar, volta automaticamente."),
    h("div", { class: "campo" }, rotuloConf, conf),
    h("div", { class: "linha-botoes" }, botao));
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    botao.disabled = true;
    try {
      await api(`/api/backups/importacoes/${imp.id}/aplicar`, { method: "POST", body: {
        solucao: sel.value, chave: banco ? "" : pasta.value, juntar: !banco && form.elements.juntar.checked, confirmacao: conf.value.trim() } });
      estadoBackups.logAberto = true;
      toast("Importação iniciada. Acompanhe o registro acima.");
      window.scrollTo({ top: 0, behavior: "smooth" });
      await atualizarBackups();
    } catch (e) { toast(e.message, "erro"); botao.disabled = false; }
  });
  return h("section", { class: `painel importacao${destacado ? " destacado" : ""}` },
    h("div", { class: "linha-titulo" }, h("h3", {}, imp.arquivo), descartar),
    h("p", { class: "nota sem-topo" }, `${detalhe} · recebido ${dataIso(imp.recebido_em).toLowerCase()}`),
    form);
}

function pintarAjustesBackup() {
  const d = estadoBackups.dados;
  const aj = d.ajustes;
  const alvo = document.getElementById("backup-corpo");
  const check = (nome, rotulo, ajuda, extra = {}) => h("label", { class: "campo-check" },
    h("input", { type: "checkbox", name: nome, checked: !!aj[nome], ...extra }),
    h("span", {}, h("strong", {}, rotulo), ajuda ? h("small", {}, ` ${ajuda}`) : null));
  const numero = (nome, rotulo, ajuda, extra = {}) => h("div", { class: "campo" },
    h("label", { for: `aj-${nome}` }, rotulo),
    h("input", { type: "number", id: `aj-${nome}`, name: nome, value: aj[nome], min: 0, max: 3650, ...extra }),
    ajuda ? h("small", {}, ajuda) : null);
  const descreve = (s) => [s.banco ? "banco do servidor" : null, s.volumes.length ? `arquivos (${s.volumes.join(", ")})` : null,
    s.externo ? `banco externo (${s.externo})` : null].filter(Boolean).join(", ") || "nada para copiar";
  const solucoes = d.solucoes.map((s) => h("label", { class: "campo-check" },
    h("input", { type: "checkbox", name: "sol", value: s.id, checked: s.manifesto_inclui && !aj.excluir.includes(s.id), disabled: !s.manifesto_inclui }),
    h("span", {}, h("strong", {}, s.nome), h("small", {}, ` ${s.manifesto_inclui ? descreve(s) : "fora do backup pelo manifesto ([backup] incluir = false)"}`))));

  const form = h("form", { class: "config ajustes-backup" },
    h("section", { class: "painel" }, h("h2", {}, "Quando"),
      check("ativo", "Backup automático ligado", "Roda dentro do portal, todo dia, nos horários abaixo."),
      h("div", { class: "campo" }, h("label", { for: "aj-horarios" }, "Horários"),
        h("input", { type: "text", id: "aj-horarios", name: "horarios", value: aj.horarios.join(", "), placeholder: "02:30" }),
        h("small", {}, `Um ou mais, separados por vírgula (ex.: 02:30, 13:00). Horário de Brasília. Se a VM estiver desligada no horário, o backup sai quando ela voltar (até 12 horas depois). Além destes, o portal faz um backup antes de cada atualização (deploy/atualizar.sh).`))),
    h("section", { class: "painel" }, h("h2", {}, "Quanto tempo guardar"),
      h("div", { class: "form-grade quatro" },
        numero("manter_diarios", "Dias", "Todos os automáticos destes últimos dias.", { min: 1 }),
        numero("manter_semanais", "Semanas", "Um por semana."),
        numero("manter_mensais", "Meses", "Um por mês."),
        numero("manter_avulsos", "Manuais", "Os mais recentes: manuais, antes de atualizar e de segurança.")),
      h("p", { class: "nota" }, "O último backup completo nunca é apagado, e os fixados também não.")),
    h("section", { class: "painel" }, h("h2", {}, "O que copiar"),
      check("incluir_bancos", "Bancos do PostgreSQL do servidor", d.postgres ? "Todos, inclusive os criados à mão no pgAdmin." : "Este servidor não tem o PostgreSQL do portal.", { disabled: !d.postgres }),
      check("incluir_volumes", "Arquivos das soluções", "As pastas de dados (volumes) de cada solução, como planilhas importadas e uploads."),
      check("incluir_supabase", "Supabase dos sistemas", "Banco, usuários do login, arquivos e chaves do Supabase de cada sistema (como o Panorama). Para a cópia ficar consistente, o Supabase de cada sistema para por alguns segundos durante o backup."),
      check("incluir_externos", "Bancos externos", "O banco de uma solução fora do servidor (ex.: Supabase), quando o manifesto indica em [backup] banco_externo."),
      check("incluir_portal", "Dados do portal", "Usuários, acessos, histórico, configurações e senhas das soluções."),
      h("h3", { class: "topo-16" }, "Soluções"),
      h("div", { class: "lista-checks" }, solucoes)),
    h("section", { class: "painel" }, h("h2", {}, "Segurança"),
      check("verificar", "Conferir cada banco ao copiar", "Lê o índice de cada cópia de banco logo depois de gravar, para garantir que o arquivo está inteiro."),
      numero("espaco_minimo_gb", "Espaço livre mínimo (GB)", "Abaixo disso o backup não roda e o aviso aparece aqui.", { step: "0.5", max: 10000 }),
      check("copiar", "Gravar uma segunda cópia", d.copia ? `Em ${d.copia}.` : "Indisponível: defina PORTAL_BACKUP_COPIA_HOST no .env do servidor (ex.: uma pasta de rede montada na VM) e rode deploy/atualizar.sh.", { disabled: !d.copia }),
      h("div", { class: "campo" }, h("label", { for: "aj-webhook" }, "Avisar quando falhar (opcional)"),
        h("input", { type: "url", id: "aj-webhook", name: "aviso_webhook", value: aj.aviso_webhook, placeholder: "https://…" }),
        h("small", {}, "Endereço de webhook (ex.: um fluxo do Teams ou do Power Automate) que recebe { \"text\": \"…\" } quando um backup falha. Serve para automatizar informes de erro."))),
    h("div", { class: "linha-botoes" },
      h("button", { type: "submit", class: "botao primario" }, "Salvar ajustes"),
      d.personalizado ? h("button", { type: "button", class: "botao sutil", onclick: async () => {
        if (!(await confirmar({ titulo: "Voltar aos padrões?", texto: "Os ajustes voltam aos valores do servidor (.env). Os backups existentes não mudam.", botao: "Voltar aos padrões" }))) return;
        try { await api("/api/backups/ajustes", { method: "PUT", body: { ajustes: null } }); toast("Ajustes voltaram ao padrão."); await telaBackups(); }
        catch (e) { toast(e.message, "erro"); }
      } }, "Voltar aos padrões do servidor") : null));

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const val = (n) => form.elements[n];
    const ajustes = {
      ativo: val("ativo").checked, horarios: val("horarios").value,
      manter_diarios: Number(val("manter_diarios").value), manter_semanais: Number(val("manter_semanais").value),
      manter_mensais: Number(val("manter_mensais").value), manter_avulsos: Number(val("manter_avulsos").value),
      incluir_bancos: val("incluir_bancos").checked, incluir_volumes: val("incluir_volumes").checked,
      incluir_externos: val("incluir_externos").checked, incluir_supabase: val("incluir_supabase").checked, incluir_portal: val("incluir_portal").checked,
      verificar: val("verificar").checked, espaco_minimo_gb: val("espaco_minimo_gb").value,
      copiar: val("copiar").checked, aviso_webhook: val("aviso_webhook").value.trim(),
      excluir: d.solucoes.filter((s) => s.manifesto_inclui && !form.querySelector(`[name=sol][value="${s.id}"]`).checked).map((s) => s.id),
    };
    try {
      await api("/api/backups/ajustes", { method: "PUT", body: { ajustes } });
      toast("Ajustes salvos.");
      estadoBackups.dados = await api("/api/backups");
      pintarResumoBackup();
      pintarAjustesBackup();
    } catch (e) { toast(e.message, "erro"); }
  });
  alvo.replaceChildren(form);
}

/* ================= admin: alertas de saúde ================= */

const estadoAlertas = { aba: "alertas", dados: null };

function dataCurta(ts) {
  return new Date(ts * 1000).toLocaleDateString("pt-BR");
}

function duracaoTexto(s) {
  s = Math.max(0, Math.round(s));
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`;
}

/** Número de alertas ativos ao lado de "Alertas" no menu (só administradores). */
async function atualizarContadorAlertas() {
  const alvo = document.getElementById("menu-alertas-n");
  if (!alvo) return;
  try {
    const { ativos } = await api("/api/alertas/resumo");
    alvo.textContent = ativos ? String(ativos) : "";
    alvo.hidden = !ativos;
    alvo.title = ativos ? plural(ativos, "alerta ativo", "alertas ativos") : "";
  } catch { /* fica como está */ }
}

async function telaAlertas() {
  document.title = "Alertas · Portal de Soluções LOG20";
  estadoAlertas.dados = await api("/api/alertas");
  const abas = [["alertas", "Alertas"], ["ajustes", "Ajustes"]];
  $conteudo.replaceChildren(
    h("section", { class: "intro intro-linha" },
      h("div", {}, h("h1", {}, "Alertas"),
        h("p", {}, "O portal confere a saúde do servidor a cada minuto e avisa os administradores quando algo dá errado.")),
      h("div", { class: "intro-lado" },
        h("button", { type: "button", class: "botao", onclick: enviarSituacao,
          title: "Alertas ativos, estado de cada sistema, espaço em disco e último backup" }, "Enviar situação atual por e-mail"))),
    h("div", { id: "alertas-aviso" }),
    h("div", { class: "abas", role: "tablist" }, abas.map(([k, rotulo]) =>
      h("button", { type: "button", role: "tab", "aria-selected": String(estadoAlertas.aba === k),
        onclick: () => { estadoAlertas.aba = k; telaAlertas(); } }, rotulo))),
    h("div", { id: "alertas-corpo" }));
  pintarAvisoAlertas();
  if (estadoAlertas.aba === "ajustes") pintarAjustesAlertas();
  else pintarListaAlertas();
}

async function enviarSituacao(ev) {
  const botao = ev.currentTarget;
  const para = estadoAlertas.dados?.ajustes.email_para || [];
  if (!para.length) { toast("Informe os destinatários em Ajustes → E-mail e salve.", "erro"); return; }
  if (!(await confirmar({ titulo: "Enviar a situação atual?",
    texto: `Um e-mail agora para ${para.join(", ")}, com os alertas ativos, o estado de cada sistema, o espaço em disco e o último backup.`,
    botao: "Enviar" }))) return;
  botao.disabled = true;
  try { toast((await api("/api/alertas/enviar-situacao", { method: "POST" })).mensagem); }
  catch (e) { toast(e.message, "erro"); }
  botao.disabled = false;
}

async function atualizarAlertas() {
  const antes = estadoAlertas.dados;
  estadoAlertas.dados = await api("/api/alertas");
  pintarAvisoAlertas();
  const d = estadoAlertas.dados;
  if (estadoAlertas.aba === "alertas" && JSON.stringify([antes?.ativos, antes?.historico, antes?.ultima_verificacao])
      !== JSON.stringify([d.ativos, d.historico, d.ultima_verificacao])) pintarListaAlertas();
  atualizarContadorAlertas();
}

function pintarAvisoAlertas() {
  const d = estadoAlertas.dados;
  const aj = d.ajustes;
  const msgs = [];
  if (!aj.ativo) msgs.push(h("li", {}, "Os alertas estão desligados (Ajustes)."));
  else if (!aj.email_ativo) msgs.push(h("li", {}, "O envio por e-mail está desligado: os alertas só aparecem nesta tela. Ligue em Ajustes."));
  if (d.erro_envio) msgs.push(h("li", {}, `O último aviso não foi enviado (${quando(d.erro_envio_em)}): ${d.erro_envio}`));
  if (d.erro) msgs.push(h("li", {}, `Erro na verificação: ${d.erro}`));
  document.getElementById("alertas-aviso").replaceChildren(
    msgs.length ? h("div", { class: "aviso" }, h("strong", {}, "Atenção"), h("ul", {}, msgs)) : null);
}

function nomeAlvoAlerta(a) {
  return estadoAlertas.dados.nomes[a.alvo] || "";
}

function pintarListaAlertas() {
  const d = estadoAlertas.dados;
  const agora = Date.now() / 1000;
  const ativos = d.ativos.length
    ? h("div", { class: "lista-alertas" }, d.ativos.map((a) => h("div", { class: "aviso-item erro" },
        h("span", { class: "aviso-nivel" }, `${d.tipos[a.tipo] || a.tipo} · desde ${quando(a.inicio)} (${duracaoTexto(agora - a.inicio)})`),
        h("strong", {}, a.titulo),
        h("p", {}, a.detalhe),
        d.nomes[a.alvo] ? h("p", {}, h("a", { href: `#/admin/solucao/${a.alvo}` }, `Abrir ${d.nomes[a.alvo]}`)) : null)))
    : h("div", { class: "vazio vazio-lado" }, h("strong", {}, "Nenhum alerta ativo"),
        h("p", {}, d.ajustes.ativo
          ? (d.ultima_verificacao ? `Última verificação: ${quando(d.ultima_verificacao)}.` : "A primeira verificação sai cerca de um minuto depois que o portal inicia.")
          : "Os alertas estão desligados."));
  const historico = d.historico.length
    ? h("div", { class: "tabela-rolagem" }, h("table", { class: "tabela-simples tabela-alertas" },
        h("thead", {}, h("tr", {}, h("th", {}, "Alerta"), h("th", {}, "Começou"), h("th", {}, "Resolvido"), h("th", {}, "Duração"))),
        h("tbody", {}, d.historico.map((a) => h("tr", {},
          h("td", {}, h("strong", {}, a.titulo), h("br"), h("small", { class: "suave" }, a.detalhe)),
          h("td", {}, quando(a.inicio)), h("td", {}, quando(a.fim)), h("td", {}, duracaoTexto(a.fim - a.inicio)))))))
    : h("p", { class: "nota" }, "Nenhum alerta resolvido ainda.");
  const c = d.certificado;
  const painelHttps = c && c.configurado ? h("section", { class: "painel" }, h("h2", {}, "HTTPS"),
    c.ok
      ? h("p", {}, h("strong", {}, `https://${c.nome}`), ` · certificado válido até ${dataCurta(c.valido_ate)} (${plural(c.dias, "dia", "dias")})`,
          h("br"), h("small", { class: "suave" }, `Vale para: ${c.nomes.join(", ")}${c.emissor ? ` · emitido por ${c.emissor}` : ""}`))
      : h("p", {}, h("strong", {}, "Desligado: "), c.erro, " Os sistemas respondem só em http://, pelo IP.")) : null;
  document.getElementById("alertas-corpo").replaceChildren(
    h("section", { class: "painel" }, h("h2", {}, `Ativos (${d.ativos.length})`), ativos),
    painelHttps,
    h("section", { class: "painel" }, h("h2", {}, "Histórico"), historico,
      h("p", { class: "nota" }, "Os últimos 100 alertas resolvidos; guardados por 180 dias.")));
}

function pintarAjustesAlertas() {
  const d = estadoAlertas.dados;
  const aj = d.ajustes;
  const check = (nome, rotulo, ajuda, marcado) => h("label", { class: "campo-check" },
    h("input", { type: "checkbox", name: nome, checked: marcado ?? !!aj[nome] }),
    h("span", {}, h("strong", {}, rotulo), ajuda ? h("small", {}, ` ${ajuda}`) : null));
  const numero = (nome, rotulo, ajuda, extra = {}) => h("div", { class: "campo" },
    h("label", { for: `al-${nome}` }, rotulo),
    h("input", { type: "number", id: `al-${nome}`, name: nome, value: aj[nome], ...extra }),
    ajuda ? h("small", {}, ajuda) : null);
  const texto = (nome, rotulo, ajuda, extra = {}) => h("div", { class: "campo" },
    h("label", { for: `al-${nome}` }, rotulo),
    h("input", { type: "text", id: `al-${nome}`, name: nome, value: aj[nome] ?? "", autocomplete: "off", spellcheck: false, ...extra }),
    ajuda ? h("small", {}, ajuda) : null);
  const tipo = (t, ajuda) => check(`tipo-${t}`, d.tipos[t], ajuda, aj.tipos[t] !== false);

  const origem = h("select", { id: "al-smtp_origem", name: "smtp_origem" },
    d.origens_smtp.map((o) => h("option", { value: o.id, selected: o.id === aj.smtp_origem }, o.servidor ? `${o.nome} (${o.servidor})` : o.nome)));
  const proprio = h("div", { class: "grupo-smtp" },
    h("div", { class: "form-grade" },
      texto("smtp_servidor", "Servidor de e-mail (SMTP)", "", { placeholder: "smtp.office365.com" }),
      numero("smtp_porta", "Porta", "587 (STARTTLS) ou 465 (SSL).", { min: 1, max: 65535 }),
      texto("smtp_usuario", "Usuário", "")),
    h("div", { class: "form-grade" },
      texto("smtp_senha", "Senha", aj.tem_senha ? "Em branco mantém a senha guardada." : "",
        { type: "password", autocomplete: "new-password", value: "", placeholder: aj.tem_senha ? "•••••••• (guardada)" : "" }),
      texto("smtp_remetente", "E-mail do remetente", "", { inputmode: "email", placeholder: "alertas@log20.com.br" }),
      texto("smtp_nome", "Nome do remetente", "", { placeholder: "Portal LOG20" })));
  const mostrarProprio = () => { proprio.hidden = origem.value !== "proprio"; };
  origem.addEventListener("change", mostrarProprio);
  mostrarProprio();

  const erro = h("p", { class: "form-erro", role: "alert", hidden: true });
  const ok = h("p", { class: "nota", role: "status", hidden: true });
  const form = h("form", { class: "config ajustes-backup" },
    h("section", { class: "painel" }, h("h2", {}, "O que vigiar"),
      check("ativo", "Alertas ligados", "O portal confere a cada minuto. Sistemas parados de propósito não geram alerta."),
      h("div", { class: "lista-checks" },
        tipo("sistema", "Um sistema que deveria estar no ar cai ou não responde (inclui o Supabase dele)."),
        tipo("reinicios", "3 ou mais reinícios em 30 minutos, ou parada por falta de memória."),
        tipo("memoria", "Um sistema perto do limite de memória definido em Uso."),
        tipo("disco", "Pouco espaço livre no disco do portal ou da pasta de backup."),
        tipo("backup", "O último backup falhou, não foi feito ou está atrasado."),
        tipo("copia", "A segunda cópia falhou, ou a pasta de rede não está montada (a cópia cairia no disco da VM)."),
        tipo("certificado", "Com HTTPS: o certificado vai vencer, venceu ou tem problema (o portal volta para HTTP).")),
      h("h3", { class: "topo-16" }, "Limites"),
      h("div", { class: "form-grade quatro" },
        numero("minutos_fora", "Fora do ar há (min)", "Antes de avisar. Evita alerta por uma reinicialização rápida.", { min: 1, max: 120 }),
        numero("memoria_pct", "Memória acima de (%)", "Do limite de cada sistema.", { min: 50, max: 100 }),
        numero("memoria_minutos", "Por (min)", "Quanto tempo acima do limite antes de avisar.", { min: 1, max: 120 }),
        numero("backup_horas", "Backup atrasado após (h)", "Desde o último backup completo.", { min: 2, max: 336 })),
      h("div", { class: "form-grade quatro" },
        numero("disco_min_gb", "Disco livre mínimo (GB)", "", { min: 0, step: "0.5" }),
        numero("disco_min_pct", "ou mínimo (%)", "Avisa quando qualquer um dos dois for atingido.", { min: 0, max: 90 }),
        numero("certificado_dias", "Certificado vence em (dias)", "Avisa quando faltar menos que isso.", { min: 1, max: 180 }))),
    h("section", { class: "painel" }, h("h2", {}, "Avisos"),
      h("div", { class: "form-grade quatro" },
        numero("lembrar_horas", "Lembrar a cada (h)", "Enquanto o problema continuar. 0 = não lembrar.", { min: 0, max: 168 })),
      check("avisar_resolvido", "Avisar quando resolver", "Uma mensagem quando o problema acaba, com quanto tempo durou."),
      h("h3", { class: "topo-16" }, "E-mail"),
      check("email_ativo", "Enviar por e-mail", ""),
      h("div", { class: "campo" }, h("label", { for: "al-email_para" }, "Destinatários"),
        h("input", { type: "text", id: "al-email_para", name: "email_para", value: (aj.email_para || []).join(", "), placeholder: "ti@log20.com.br, gustavo@log20.com.br", spellcheck: false }),
        h("small", {}, "Separados por vírgula. Pode ser uma lista de distribuição.")),
      h("div", { class: "campo" }, h("label", { for: "al-smtp_origem" }, "Servidor de e-mail"), origem,
        h("small", {}, "Pode usar o mesmo e-mail configurado no login de um sistema (Gerenciar → Supabase → Login e e-mail).")),
      proprio),
    erro, ok,
    h("div", { class: "linha-botoes" },
      h("button", { type: "submit", class: "botao primario" }, "Salvar ajustes"),
      h("button", { type: "button", class: "botao", onclick: async (ev) => {
        const botao = ev.currentTarget;
        botao.disabled = true; erro.hidden = true; ok.hidden = true;
        try {
          const r = await api("/api/alertas/testar", { method: "POST", body: { ajustes: valores() } });
          ok.textContent = r.mensagem; ok.hidden = false;
        } catch (e) { erro.textContent = e.message; erro.hidden = false; }
        botao.disabled = false;
      } }, "Enviar e-mail de teste")),
    h("p", { class: "nota" }, "O portal não vigia a si mesmo: se a VM ou o portal cair, ninguém é avisado. Para isso, um monitor de fora (ex.: o da TI) pode consultar ",
      h("code", {}, `${location.origin}/api/saude`), ", que responde sem login."));

  const valores = () => {
    const v = (n) => form.elements[n];
    return {
      ativo: v("ativo").checked,
      tipos: Object.fromEntries(Object.keys(d.tipos).map((t) => [t, v(`tipo-${t}`).checked])),
      minutos_fora: v("minutos_fora").value, memoria_pct: v("memoria_pct").value, memoria_minutos: v("memoria_minutos").value,
      backup_horas: v("backup_horas").value, disco_min_gb: v("disco_min_gb").value, disco_min_pct: v("disco_min_pct").value,
      certificado_dias: v("certificado_dias").value,
      lembrar_horas: v("lembrar_horas").value, avisar_resolvido: v("avisar_resolvido").checked,
      email_ativo: v("email_ativo").checked, email_para: v("email_para").value, smtp_origem: origem.value,
      smtp_servidor: v("smtp_servidor").value.trim(), smtp_porta: v("smtp_porta").value.trim(), smtp_usuario: v("smtp_usuario").value.trim(),
      smtp_senha: v("smtp_senha").value, smtp_remetente: v("smtp_remetente").value.trim(), smtp_nome: v("smtp_nome").value.trim(),
    };
  };
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    erro.hidden = true; ok.hidden = true;
    try {
      await api("/api/alertas/ajustes", { method: "PUT", body: { ajustes: valores() } });
      toast("Ajustes dos alertas salvos.");
      estadoAlertas.dados = await api("/api/alertas");
      pintarAvisoAlertas();
      pintarAjustesAlertas();
    } catch (e) { erro.textContent = e.message; erro.hidden = false; }
  });
  document.getElementById("alertas-corpo").replaceChildren(form);
}

/* ================= admin: uso de recursos ================= */

const estadoUso = { horas: 24, dados: null, ocultas: new Set() };
const PERIODOS_USO = [[1, "1 hora"], [6, "6 horas"], [24, "24 horas"], [168, "7 dias"]];

function bytesTexto(n) {
  if (n == null) return "—";
  const gb = n / 1024 ** 3;
  if (gb >= 1) return `${gb.toLocaleString("pt-BR", { maximumFractionDigits: gb >= 10 ? 0 : 1 })} GB`;
  return `${Math.round(n / 1024 ** 2).toLocaleString("pt-BR")} MB`;
}
function mbTexto(mb) { return mb ? bytesTexto(mb * 1024 ** 2) : "sem limite"; }
function nucleos(n) {
  if (!n) return "sem limite";
  return `${n.toLocaleString("pt-BR", { maximumFractionDigits: 2 })} ${n === 1 ? "núcleo" : "núcleos"}`;
}
function porcento(x) { return `${Math.round(x * 100)}%`; }

/** SVG com a mesma assinatura do h(). */
function svg(tag, attrs = {}, ...filhos) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) el.setAttribute(k, v);
  for (const f of filhos.flat()) if (f != null) el.append(f);
  return el;
}

/** Medidor: a cor diz a gravidade (normal, atenção, crítico); o texto ao lado repete o número. */
function medidor(fracao, { atencao = 0.8, critico = 0.95 } = {}) {
  const f = Math.max(0, Math.min(1, fracao || 0));
  const nivel = f >= critico ? "critico" : f >= atencao ? "atencao" : "normal";
  return h("span", { class: `medidor ${nivel}`, role: "meter", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(f * 100)) },
    h("span", { style: `width:${(f * 100).toFixed(1)}%` }));
}

/** Escala "bonita" para o eixo: 0 até um valor redondo acima do máximo. */
function escala(max, passos = 4) {
  if (!(max > 0)) return { max: 1, ticks: [0, 0.25, 0.5, 0.75, 1] };
  const bruto = max / passos;
  const mag = 10 ** Math.floor(Math.log10(bruto));
  const passo = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((p) => p >= bruto);
  const topo = passo * Math.ceil(max / passo);
  const ticks = [];
  for (let v = 0; v <= topo + passo / 2; v += passo) ticks.push(v);
  return { max: topo, ticks };
}

/**
 * Gráfico de linhas com legenda, linha-guia e leitura de todas as séries no ponto (mouse,
 * toque ou setas do teclado). series: [{id, nome, cor (índice 1-8 ou 'outros'), valores}]
 */
function graficoLinhas({ titulo, subtitulo, tempos, series, formatar, formatarEixo, horas }) {
  const caixa = h("figure", { class: "grafico" });
  const legenda = h("ul", { class: "legenda" }, series.map((s) =>
    h("li", {}, h("span", { class: `chave serie-${s.cor}` }), s.nome)));
  const area = h("div", { class: "grafico-area", tabindex: "0", role: "img",
    "aria-label": `${titulo}. Use as setas para ler os valores. Os números de cada sistema também estão na tabela abaixo.` });
  const dica = h("div", { class: "dica-grafico", hidden: true });
  caixa.append(h("figcaption", {}, h("strong", {}, titulo), subtitulo ? h("span", {}, subtitulo) : null), legenda, area, dica);

  let indice = null;
  const desenhar = () => {
    const largura = Math.max(280, area.clientWidth || 600);
    const altura = 220;
    const m = { e: 56, d: 14, t: 10, b: 26 };
    const w = largura - m.e - m.d, alt = altura - m.t - m.b;
    const n = tempos.length;
    const maximo = Math.max(0, ...series.flatMap((s) => s.valores.filter((v) => v != null)));
    const esc = escala(maximo);
    const x = (i) => m.e + (n <= 1 ? w / 2 : (i * w) / (n - 1));
    const y = (v) => m.t + alt - (v / esc.max) * alt;
    const raiz = svg("svg", { width: largura, height: altura, viewBox: `0 0 ${largura} ${altura}`, "aria-hidden": "true" });
    for (const tk of esc.ticks) {
      raiz.append(svg("line", { class: tk === 0 ? "base" : "grade", x1: m.e, x2: largura - m.d, y1: y(tk), y2: y(tk) }));
      raiz.append(svg("text", { class: "eixo", x: m.e - 8, y: y(tk) + 4, "text-anchor": "end" }, formatarEixo(tk, esc.max)));
    }
    const nRotulos = Math.min(n, largura < 520 ? 3 : 6);
    for (let k = 0; k < nRotulos; k++) {
      const i = nRotulos === 1 ? 0 : Math.round((k * (n - 1)) / (nRotulos - 1));
      const d = new Date(tempos[i] * 1000);
      const rot = horas > 24 ? d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }) + " " + d.toLocaleTimeString("pt-BR", { hour: "2-digit" }) + "h"
        : d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
      raiz.append(svg("text", { class: "eixo", x: x(i), y: altura - 6, "text-anchor": k === 0 ? "start" : k === nRotulos - 1 ? "end" : "middle" }, rot));
    }
    for (const s of series) {
      let d = "", aberto = false;
      s.valores.forEach((v, i) => {
        if (v == null) { aberto = false; return; }
        d += `${aberto ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
        aberto = true;
      });
      raiz.append(svg("path", { class: `linha serie-${s.cor}`, d }));
    }
    const guia = svg("line", { class: "guia", y1: m.t, y2: m.t + alt, visibility: "hidden" });
    const pontos = svg("g", {});
    raiz.append(guia, pontos);
    area.replaceChildren(raiz);

    const mostrar = (i) => {
      indice = i;
      if (i == null) { guia.setAttribute("visibility", "hidden"); pontos.replaceChildren(); dica.hidden = true; return; }
      guia.setAttribute("x1", x(i)); guia.setAttribute("x2", x(i)); guia.setAttribute("visibility", "visible");
      pontos.replaceChildren(...series.filter((s) => s.valores[i] != null).map((s) =>
        svg("circle", { class: `ponto serie-${s.cor}`, cx: x(i), cy: y(s.valores[i]), r: 4 })));
      const linhas = series.filter((s) => s.valores[i] != null).sort((a, b) => b.valores[i] - a.valores[i]);
      const d = new Date(tempos[i] * 1000);
      dica.replaceChildren(
        h("div", { class: "dica-quando" }, d.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })),
        ...linhas.map((s) => h("div", { class: "dica-linha" }, h("span", { class: `chave serie-${s.cor}` }), h("strong", {}, formatar(s.valores[i])), h("span", {}, s.nome))));
      dica.hidden = false;
      const px = x(i) + 14;
      const larguraDica = dica.offsetWidth || 180;
      dica.style.left = `${area.offsetLeft + (px + larguraDica > largura ? Math.max(0, x(i) - larguraDica - 14) : px)}px`;
      dica.style.top = `${area.offsetTop + 8}px`;
    };
    const indicePara = (clientX) => {
      const r = raiz.getBoundingClientRect();
      const rel = (clientX - r.left - m.e) / w;
      return Math.max(0, Math.min(n - 1, Math.round(rel * (n - 1))));
    };
    area.onpointermove = (ev) => mostrar(indicePara(ev.clientX));
    area.onpointerleave = () => mostrar(null);
    area.onblur = () => mostrar(null);
    area.onkeydown = (ev) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(ev.key)) return;
      ev.preventDefault();
      const atual = indice ?? n - 1;
      mostrar(ev.key === "Home" ? 0 : ev.key === "End" ? n - 1 : Math.max(0, Math.min(n - 1, atual + (ev.key === "ArrowLeft" ? -1 : 1))));
    };
    area.onfocus = () => mostrar(n - 1);
    if (indice != null) mostrar(Math.min(indice, n - 1));
  };
  if (!tempos.length) {
    area.replaceChildren(h("p", { class: "nota grafico-vazio" }, "Ainda sem dados: a coleta começou agora. Os primeiros pontos aparecem em alguns minutos."));
  } else {
    requestAnimationFrame(desenhar);
    new ResizeObserver(() => desenhar()).observe(area);
  }
  return caixa;
}

async function telaUso() {
  document.title = "Uso · Portal de Soluções LOG20";
  estadoUso.dados = await api(`/api/uso?horas=${estadoUso.horas}`);
  $conteudo.replaceChildren(
    h("section", { class: "intro intro-linha" },
      h("div", {}, h("h1", {}, "Uso"),
        h("p", {}, "Memória, CPU e disco do servidor e de cada sistema. Cada sistema tem um limite: se passar do limite de memória, só ele é reiniciado, sem derrubar os outros.")),
      h("div", { class: "intro-lado" },
        h("div", { class: "periodo", role: "group", "aria-label": "Período dos gráficos" }, PERIODOS_USO.map(([hs, rotulo]) =>
          h("button", { type: "button", class: "botao pequeno", "aria-pressed": String(estadoUso.horas === hs),
            onclick: async () => { estadoUso.horas = hs; await telaUso(); } }, rotulo))))),
    h("div", { id: "uso-alertas" }),
    h("div", { class: "grade-backup", id: "uso-cartoes" }),
    h("div", { class: "graficos-uso", id: "uso-graficos" }),
    h("section", { class: "painel" }, h("h2", {}, "Por sistema"),
      h("p", { class: "nota sem-topo" }, "Valores de agora, o pico das últimas 24 horas e o espaço em disco de cada sistema. Os gráficos acima mostram a mesma informação ao longo do tempo."),
      h("div", { id: "uso-tabela" })),
    h("div", { class: "uso-rodape" },
      h("section", { class: "painel" }, h("h2", {}, "Espaço em disco"), h("div", { id: "uso-disco" })),
      h("section", { class: "painel" }, h("h2", {}, "Reinícios e falta de memória"), h("div", { id: "uso-eventos" }))));
  pintarUso(true);
}

async function atualizarUso() {
  if (document.querySelector("dialog[open]")) return;
  estadoUso.dados = await api(`/api/uso?horas=${estadoUso.horas}`);
  pintarUso(true);
}

function pintarUso(graficos) {
  const d = estadoUso.dados;
  const maq = d.maquina;
  const solucoes = d.alvos.filter((a) => a.tipo === "solucao");
  const fracMem = maq.mem_total ? maq.mem_usada / maq.mem_total : 0;
  const fracCpu = Math.min(1, (maq.carga[0] || 0) / (maq.cpus || 1));
  const fracDisco = maq.disco_total ? maq.disco_usado / maq.disco_total : 0;
  const docker = d.tamanhos.docker || {};
  const recuperavel = (docker["Build Cache"]?.recuperavel || 0) + (docker.Images?.recuperavel || 0);

  // Alertas: o que precisa de atenção agora.
  const alertas = [];
  if (fracDisco >= 0.85) alertas.push(`Disco da VM ${porcento(fracDisco)} cheio (${bytesTexto(maq.disco_livre)} livres).${recuperavel > 1024 ** 3 ? " Libere espaço no servidor usando o botão abaixo." : ""}`);
  if (fracMem >= 0.9) alertas.push(`Memória da VM ${porcento(fracMem)} usada. Considere limites menores ou mais memória para a VM.`);
  for (const a of solucoes) {
    const lim = a.limites.memoria_mb * 1024 ** 2;
    if (a.mem && lim && a.mem / lim >= 0.9) alertas.push(`${a.nome} está usando ${porcento(a.mem / lim)} do limite de memória (${mbTexto(a.limites.memoria_mb)}). Perto do limite, ele é reiniciado.`);
  }
  const oom = d.historico.eventos.filter((e) => e.tipo === "falta_memoria" && Date.now() / 1000 - e.ts < 86400);
  for (const e of oom) alertas.push(`${solucoes.find((s) => s.id === e.alvo)?.nome || e.alvo} foi reiniciado por falta de memória (${quando(e.ts).toLowerCase()}). Aumente o limite dele.`);
  if (d.coleta.erro) alertas.push(`A coleta teve um problema: ${d.coleta.erro}`);
  document.getElementById("uso-alertas").replaceChildren(alertas.length
    ? h("div", { class: "aviso" }, h("strong", {}, "Atenção"), h("ul", {}, alertas.map((a) => h("li", {}, a)))) : null);

  // Cartões da VM.
  const cartao = (rotulo, valor, detalhe, extra) => h("div", { class: "cartao-backup" },
    h("span", { class: "deteccao-rotulo" }, rotulo), h("strong", {}, valor), extra || null, detalhe ? h("small", {}, detalhe) : null);
  document.getElementById("uso-cartoes").replaceChildren(
    cartao("Memória da VM", `${bytesTexto(maq.mem_usada)} de ${bytesTexto(maq.mem_total)}`, `${porcento(fracMem)} em uso${maq.swap_usada > 64 * 1024 ** 2 ? ` · ${bytesTexto(maq.swap_usada)} em swap` : ""}`, medidor(fracMem, { atencao: 0.8, critico: 0.92 })),
    cartao("CPU da VM", porcento(fracCpu), `Carga média de 1 min: ${maq.carga[0].toLocaleString("pt-BR")} em ${nucleos(maq.cpus)}`, medidor(fracCpu, { atencao: 0.7, critico: 0.9 })),
    cartao("Disco da VM", `${bytesTexto(maq.disco_livre)} livres`, `${bytesTexto(maq.disco_usado)} de ${bytesTexto(maq.disco_total)} em uso`, medidor(fracDisco, { atencao: 0.8, critico: 0.92 })),
    cartao("Espaço que é possível liberar", bytesTexto(recuperavel), "Cache de construção e imagens antigas do Docker. Dados e bancos não são impactados.",
      h("button", { type: "button", class: "botao pequeno", disabled: recuperavel < 50 * 1024 ** 2, onclick: liberarEspaco }, "Liberar")));

  if (graficos) pintarGraficosUso();
  pintarTabelaUso();
  pintarDiscoUso();
  pintarEventosUso();
}

async function liberarEspaco(ev) {
  const botao = ev.currentTarget;
  if (!(await confirmar({ titulo: "Liberar espaço do Docker?", texto: "Apaga o cache de construção e as imagens sem nome (restos de versões antigas). Bancos, arquivos e sistemas em uso não mudam. A próxima atualização de um sistema pode demorar um pouco mais.", botao: "Liberar espaço" }))) return;
  botao.disabled = true;
  botao.replaceChildren(h("span", { class: "giro", "aria-hidden": "true" }), "Liberando…");
  try {
    const r = await api("/api/uso/limpar-docker", { method: "POST" });
    toast(`${r.liberado_texto} liberados.`);
    await atualizarUso();
  } catch (e) { toast(e.message, "erro"); botao.disabled = false; botao.textContent = "Liberar"; }
}

function pintarGraficosUso() {
  const d = estadoUso.dados;
  const hist = d.historico;
  // Cor segue o sistema (ordem fixa: soluções por nome, depois a infraestrutura), nunca o ranking.
  const alvos = d.alvos.filter((a) => hist.series[a.id]);
  const tempos = [...new Set(Object.values(hist.series).flatMap((s) => s.t))].sort((a, b) => a - b);
  const pos = new Map(tempos.map((t, i) => [t, i]));
  const montar = (campo, transformar) => {
    const lista = alvos.slice(0, alvos.length > 8 ? 7 : 8).map((a, i) => {
      const valores = Array(tempos.length).fill(null);
      hist.series[a.id].t.forEach((t, k) => { valores[pos.get(t)] = transformar(hist.series[a.id][campo][k]); });
      return { id: a.id, nome: a.nome, cor: i + 1, valores };
    });
    if (alvos.length > 8) {
      const valores = Array(tempos.length).fill(null);
      for (const a of alvos.slice(7)) hist.series[a.id].t.forEach((t, k) => {
        const i = pos.get(t); valores[i] = (valores[i] || 0) + transformar(hist.series[a.id][campo][k]);
      });
      lista.push({ id: "outros", nome: `Outros (${alvos.length - 7})`, cor: "outros", valores });
    }
    return lista;
  };
  const rotulo = PERIODOS_USO.find(([hs]) => hs === estadoUso.horas)[1];
  const mb = (v) => v / 1024 ** 2;
  const cpus = d.maquina.cpus || 1;
  document.getElementById("uso-graficos").replaceChildren(
    graficoLinhas({
      titulo: "Memória por sistema", subtitulo: `Últimas ${rotulo}, o maior valor de cada intervalo`, horas: estadoUso.horas,
      tempos, series: montar("mem", mb),
      formatar: (v) => bytesTexto(v * 1024 ** 2),
      formatarEixo: (v, max) => (max >= 1000 ? `${(v / 1024).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} GB` : `${Math.round(v)} MB`),
    }),
    graficoLinhas({
      titulo: "CPU por sistema", subtitulo: `Últimas ${rotulo}, % da CPU da VM (${nucleos(cpus)}), média de cada intervalo`, horas: estadoUso.horas,
      tempos, series: montar("cpu", (v) => (v / cpus) * 100),
      formatar: (v) => `${v.toLocaleString("pt-BR", { maximumFractionDigits: v < 10 ? 1 : 0 })}%`,
      formatarEixo: (v) => `${Math.round(v)}%`,
    }));
}

function pintarTabelaUso() {
  const d = estadoUso.dados;
  const linhas = d.alvos.map((a) => {
    const sol = a.tipo === "solucao";
    const limMem = sol ? a.limites.memoria_mb : 0;
    const fracMem = a.mem != null && limMem ? a.mem / (limMem * 1024 ** 2) : null;
    const disco = sol ? a.disco.volumes + a.disco.banco + a.disco.imagem : null;
    return h("tr", { class: sol ? "" : "linha-infra" },
      h("th", { scope: "row" }, h("span", { class: "nome-sistema" }, a.nome),
        sol ? h("div", { class: "sub" }, pilula(a.status.estado, a.status.detalhe))
          : h("div", { class: "sub" }, a.id.startsWith("supabase:") ? "Banco, login e arquivos do sistema" : "Infraestrutura do portal")),
      h("td", {}, a.mem != null ? h("div", { class: "valor-uso" },
          h("strong", {}, bytesTexto(a.mem)), sol ? h("span", {}, ` de ${mbTexto(limMem)}`) : null) : h("span", { class: "sub" }, "Parado"),
        fracMem != null ? medidor(fracMem, { atencao: 0.8, critico: 0.9 }) : null,
        // Container criado antes dos limites (ou com outro valor): o limite configurado ainda não vale.
        sol && a.rodando && limMem && a.mem_limite_docker && Math.abs(a.mem_limite_docker - limMem * 1024 ** 2) > 8 * 1024 ** 2
          ? h("div", { class: "sub alerta-limite" }, "Limite ainda não aplicado: clique em Limites e Salvar (aplicado instantaneamente)") : null),
      h("td", { class: "num" }, a.pico_mem_24h ? bytesTexto(a.pico_mem_24h) : "—"),
      h("td", {}, a.cpu != null ? h("div", { class: "valor-uso" },
          h("strong", {}, a.cpu.toLocaleString("pt-BR", { maximumFractionDigits: 2 })), h("span", {}, sol ? ` de ${nucleos(a.limites.cpus)}` : " núcleo")) : "—"),
      h("td", { class: "num" }, sol ? h("span", { title: `Banco ${bytesTexto(a.disco.banco)} · arquivos ${bytesTexto(a.disco.volumes)} · imagem ${bytesTexto(a.disco.imagem)}` }, bytesTexto(disco))
        : a.disco ? h("span", { title: "Banco e arquivos do Supabase do sistema" }, bytesTexto(a.disco.volumes)) : "—"),
      h("td", { class: "acoes-backup" }, sol ? h("button", { type: "button", class: "botao pequeno", onclick: () => dialogoLimites(a) }, "Limites") : null));
  });
  document.getElementById("uso-tabela").replaceChildren(h("div", { class: "tabela-rolagem" },
    h("table", { class: "tabela-simples tabela-uso" },
      h("thead", {}, h("tr", {}, ["Sistema", "Memória agora", "Pico 24 h", "CPU agora", "Disco", ""].map((x, i) => h("th", { class: i === 2 || i === 4 ? "num" : "" }, x)))),
      h("tbody", {}, linhas))));
}

function pintarDiscoUso() {
  const d = estadoUso.dados;
  const t = d.tamanhos;
  const docker = t.docker || {};
  const soma = (o) => Object.values(o || {}).reduce((s, v) => s + v, 0);
  const volumesSol = Object.entries(t.volumes || {}).filter(([k]) => k !== "log20-postgres-dados").reduce((s, [, v]) => s + v, 0);
  const itens = [
    ["Imagens dos sistemas (Docker)", docker.Images?.total ?? soma(t.imagens)],
    ["Cache de construção (Docker)", docker["Build Cache"]?.total || 0],
    ["Bancos do PostgreSQL do servidor", (t.volumes || {})["log20-postgres-dados"] ?? soma(t.bancos)],
    ["Arquivos dos sistemas (volumes)", volumesSol],
    ["Backups", t.backups || 0],
  ].filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  const maior = Math.max(1, ...itens.map(([, v]) => v));
  document.getElementById("uso-disco").replaceChildren(
    itens.length ? h("ul", { class: "barras-disco" }, itens.map(([rotulo, v]) => h("li", {},
      h("span", { class: "rotulo" }, rotulo),
      h("span", { class: "trilho" }, h("span", { class: "barra", style: `width:${Math.max(1.5, (v / maior) * 100).toFixed(1)}%` })),
      h("span", { class: "valor" }, bytesTexto(v))))) : h("p", { class: "nota" }, "Medindo… (a medição dos tamanhos roda a cada 15 minutos)."),
    h("p", { class: "nota" }, `Disco da VM: ${bytesTexto(d.maquina.disco_usado)} usados, ${bytesTexto(d.maquina.disco_livre)} livres.${t.medido_em ? ` Medido ${quando(t.medido_em).toLowerCase()}.` : ""}`),
    (t.erros || []).length ? h("p", { class: "texto-erro" }, t.erros.join(" · ")) : null);
}

function pintarEventosUso() {
  const d = estadoUso.dados;
  const nomes = new Map(d.alvos.map((a) => [a.id, a.nome]));
  const eventos = d.historico.eventos;
  document.getElementById("uso-eventos").replaceChildren(eventos.length
    ? h("ul", { class: "lista-eventos" }, eventos.slice(0, 12).map((e) => h("li", {},
        pilula(e.tipo === "falta_memoria" ? "com_problema" : "ocupado", e.tipo === "falta_memoria" ? "Falta de memória" : "Reinício"),
        h("span", {}, h("strong", {}, nomes.get(e.alvo) || e.alvo), ` · ${e.detalhe}`),
        h("small", {}, quando(e.ts)))))
    : h("p", { class: "texto-ok" }, `Nenhum reinício nos últimos ${d.coleta.dias} dias.`));
}

function dialogoLimites(a) {
  const d = estadoUso.dados;
  const lim = a.limites;
  const origem = { portal: "definido aqui", manifesto: "sugerido por quem fez o sistema (manifesto)", padrao: "padrão do servidor" };
  const mem = h("input", { type: "text", id: "lim-mem", autocomplete: "off", spellcheck: false,
    value: lim.origem_memoria_mb === "portal" ? (lim.memoria_mb ? `${lim.memoria_mb}m` : "0") : "", placeholder: lim.memoria_mb ? `${lim.memoria_mb}m` : "sem limite" });
  const cpu = h("input", { type: "text", id: "lim-cpu", inputmode: "decimal", autocomplete: "off",
    value: lim.origem_cpus === "portal" ? String(lim.cpus).replace(".", ",") : "", placeholder: lim.cpus ? String(lim.cpus).replace(".", ",") : "sem limite" });
  const botao = h("button", { type: "submit", class: "botao primario", form: "form-limites" }, "Salvar");
  const form = h("form", { id: "form-limites", class: "config" },
    h("div", { class: "resumo-limites" },
      h("div", {}, h("span", { class: "deteccao-rotulo" }, "Memória agora"), h("strong", {}, a.mem != null ? bytesTexto(a.mem) : "parado")),
      h("div", {}, h("span", { class: "deteccao-rotulo" }, "Pico em 24 h"), h("strong", {}, a.pico_mem_24h ? bytesTexto(a.pico_mem_24h) : "—")),
      h("div", {}, h("span", { class: "deteccao-rotulo" }, "CPU agora"), h("strong", {}, a.cpu != null ? nucleos(a.cpu) : "—"))),
    h("div", { class: "form-grade" },
      h("div", { class: "campo" }, h("label", { for: "lim-mem" }, "Memória"), mem,
        h("small", {}, `Ex.: 512m, 1g, 1,5g. 0 = sem limite. Em branco = ${lim.origem_memoria_mb === "portal" ? "padrão" : origem[lim.origem_memoria_mb]} (${mbTexto(lim.origem_memoria_mb === "portal" ? (a.manifesto.memoria_mb ?? d.padroes.memoria_mb) : lim.memoria_mb)}).`),
        a.sugestao_mb ? h("button", { type: "button", class: "link-limpar", onclick: () => { mem.value = `${a.sugestao_mb}m`; mem.focus(); } },
          `Usar ${mbTexto(a.sugestao_mb)} (pico de 24 h + 50%)`) : null),
      h("div", { class: "campo" }, h("label", { for: "lim-cpu" }, "CPU (núcleos)"), cpu,
        h("small", {}, `Ex.: 0,5, 1, 2. A VM tem ${nucleos(d.maquina.cpus)}. 0 = sem limite. Em branco = ${lim.origem_cpus === "portal" ? "padrão" : origem[lim.origem_cpus]} (${nucleos(lim.origem_cpus === "portal" ? (a.manifesto.cpus ?? d.padroes.cpus) : lim.cpus)}).`))),
    h("ul", { class: "lista-explica" },
      h("li", {}, `Agora: memória ${mbTexto(lim.memoria_mb)} (${origem[lim.origem_memoria_mb]}), CPU ${nucleos(lim.cpus)} (${origem[lim.origem_cpus]}).`),
      h("li", {}, "Passou do limite de memória: o sistema é reiniciado sozinho (aparece em Reinícios). Passou do de CPU: ele só fica mais lento."),
      h("li", {}, "Aplicado instantaneamente, sem reiniciar, quando o sistema está no ar.")));
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    botao.disabled = true;
    try {
      const r = await api(`/api/solucoes/${a.id}/recursos`, { method: "PUT", body: {
        memoria: mem.value.trim() || null, cpus: cpu.value.trim().replace(",", ".") || null } });
      dlg.close();
      toast(`Limites de ${a.nome}: memória ${mbTexto(r.limites.memoria_mb)}, CPU ${nucleos(r.limites.cpus)}. ${r.aviso}`);
      await atualizarUso();
    } catch (e) { toast(e.message, "erro"); botao.disabled = false; }
  });
  const dlg = dialogo({
    titulo: `Limites de ${a.nome}`,
    corpo: form,
    rodape: [h("button", { type: "button", class: "botao", onclick: () => dlg.close() }, "Cancelar"), botao],
  });
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
    } else if (rota === "#/admin/backups") {
      await atualizarBackups();
    } else if (rota === "#/admin/uso") {
      await atualizarUso();
    } else if (rota === "#/admin/alertas") {
      await atualizarAlertas();
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
          : `${sistema.nome} está fora do ar no momento. Tente novamente mais tarde.`), 300);
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
    else if (rota === "#/admin/nova-solucao") telaNovaSolucao();
    else if (rota.startsWith("#/admin/editar-solucao/")) await telaEditarSolucao(rota.split("/")[3]);
    else if (rota === "#/admin/backups") { await telaBackups(); novoRitmo = [2000, 30000]; }
    else if (rota === "#/admin/uso") { await telaUso(); novoRitmo = [60000, 60000]; }
    else if (rota === "#/admin/alertas") { await telaAlertas(); novoRitmo = [30000, 30000]; }
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
