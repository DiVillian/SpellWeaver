/* SpellWeaver - the browser half.
 *
 * The form never computes anything a spell depends on. It collects a
 * definition, posts it to /api/preview, and renders what comes back. The
 * tooltip you see is therefore drawn from the same resolved values that
 * generate the SQL, so the two cannot disagree.
 */
"use strict";

const $ = (id) => document.getElementById(id);
/* Icons come out of the client's own archives, so there is no network
 * dependency and the picker shows exactly what the player will see. */
const ICON_BASE = "/api/icon/";
// What the game itself shows for "no icon chosen".
const BLANK_ICON = "INV_Misc_QuestionMark";

let VOCAB = null;
let state = { spell_id: null, behaviours: [], icon_id: 1, extra_ranks: [] };
let previewTimer = null;

/* ------------------------------------------------------------------ setup */

async function boot() {
  wireSetup();
  // Nothing else can be asked for until there is a database to ask. On a fresh
  // install that is the only thing the page can usefully show.
  const state = await ask("/api/settings");
  fillSetup(state);
  if (!state.ready) {
    showSetup();
    return;
  }
  VOCAB = await ask("/api/vocabulary");
  $("band").textContent = `ids ${VOCAB.band.start}-${VOCAB.band.end}`;

  fillSelect($("school"), VOCAB.schools, 2);
  fillSelect($("class"), VOCAB.classes, 0);
  fillLevels();
  fillTrees();
  repriceIfUntouched();
  refreshTaught();
  fillSelect($("power"), VOCAB.powers, 0);
  fillIcons();
  fillBehaviourPicker();
  fillTargets();
  fillVisuals();

  $("behaviour-add-btn").onclick = addBehaviour;
  $("rank-add").onclick = addRank;
  $("save").onclick = saveSpell;
  $("revert").onclick = revertChanges;
  $("new-ability").onclick = startNew;
  $("nav-abilities").onclick = () => { location.hash = "#new"; showBuilder(); };
  $("nav-patch").onclick = () => { location.hash = "#patch"; showPatch(); };
  $("visual-search").addEventListener("input", fillVisuals);
  $("class").onchange = () => { fillTrees(); schedulePreview(); };
  $("skill-line").onchange = schedulePreview;
  $("taught").onchange = () => { refreshTaught(); schedulePreview(); };
  $("level").addEventListener("change", () => { repriceIfUntouched(); refreshTaught(); });
  ["price-g", "price-s", "price-c"].forEach((id) => {
    $(id).addEventListener("input", () => {
      $(id).dataset.touched = "1";
      refreshTaught();
      schedulePreview();
    });
  });
  $("school").addEventListener("change", fillVisuals);
  $("ignore-mitigation").onchange = schedulePreview;
  $("patch-build").onclick = buildPatch;
  $("saved-close").onclick = () => { $("saved-overlay").hidden = true; };
  $("saved-patch").onclick = () => {
    $("saved-overlay").hidden = true;
    location.hash = "#patch";
    showPatch();
  };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("saved-overlay").hidden = true;
  });
  $("patch-refresh").onclick = () => refreshPatch();
  $("patch-restart").onclick = restartServer;
  $("target").onchange = () => { onTargetChange(); renderBehaviours(); schedulePreview(); };


  ["name", "description", "range", "radius", "cast", "cooldown",
   "duration", "school", "power", "cost", "level",
   "scaling", "spread"].forEach((id) => {
    $(id).addEventListener("input", schedulePreview);
  });
  // The same values appear in the Ranks section, so a change in one shows in
  // the other without either taking focus from the person typing.
  ["level", "cost", "power"].forEach((id) => {
    $(id).addEventListener("input", syncFirstRank);
    $(id).addEventListener("change", syncFirstRank);
  });

  paintIconPreview();
  refreshVisibility();
  window.addEventListener("hashchange", openFromHash);
  if (!openFromHash()) schedulePreview();
}

/* Create and edit are separate places: #new is always an empty form, and
 * #spell/950000 is that ability open for changes. */
function startNew() {
  if (state.dirty && !confirm(
    "This ability has unsaved changes. Start a new one and lose them?")) return;
  resetForm();
  if (location.hash === "#new") showBuilder();
  else location.hash = "#new";
}

function openFromHash() {
  if (location.hash === "#new") {
    resetForm();
    showBuilder();
    return true;
  }
  if (location.hash === "#spells") {
    showBuilder();
    return true;
  }
  if (location.hash === "#patch") {
    showPatch();
    return true;
  }
  if (location.hash === "#settings") {
    showSetup();
    return true;
  }
  const match = /^#spell\/(\d+)$/.exec(location.hash);
  if (!match) return false;
  loadSpell(Number(match[1]));
  return true;
}

function fillSelect(el, options, selected) {
  el.innerHTML = "";
  for (const o of options) {
    const opt = document.createElement("option");
    opt.value = o.value;
    opt.textContent = o.label;
    if (o.value === selected) opt.selected = true;
    el.appendChild(opt);
  }
}

/* Over 3,000 icons is far too many for a dropdown, and a dropdown cannot be
 * searched anyway. Icons are chosen by eye, so this is a filtered grid of
 * thumbnails. Only the first RENDER_LIMIT matches are drawn, because each one
 * is an image request. */
const RENDER_LIMIT = 60;

function fillIcons() {
  const sorted = VOCAB.icons.slice().sort((a, b) => a.name.localeCompare(b.name));
  VOCAB.icons = sorted;
  state.icon_id = blankIcon();
  $("icon-search").addEventListener("input", renderIconGrid);
  renderIconGrid();
}

function blankIcon() {
  const found = (VOCAB.icons || []).find(
    (i) => i.name.toLowerCase() === BLANK_ICON.toLowerCase());
  return found ? found.id : ((VOCAB.icons || [])[0] || {}).id || 1;
}

function renderIconGrid() {
  const query = $("icon-search").value.trim().toLowerCase();
  const host = $("icon-grid");
  const matches = query
    ? VOCAB.icons.filter((i) => i.name.toLowerCase().includes(query))
    : VOCAB.icons;
  host.innerHTML = "";
  if (!matches.length) {
    host.innerHTML = '<div class="none">No icon matches that.</div>';
    return;
  }
  for (const icon of matches.slice(0, RENDER_LIMIT)) {
    const button = document.createElement("button");
    button.type = "button";
    button.title = icon.name;
    button.setAttribute("aria-pressed", String(icon.id === state.icon_id));
    paintIcon(button, icon.name);
    button.onclick = () => {
      state.icon_id = icon.id;
      renderIconGrid();
      paintIconPreview();
      schedulePreview();
    };
    host.appendChild(button);
  }
}

function fillBehaviourPicker() {
  const el = $("behaviour-add");
  el.innerHTML = "";
  for (const cat of VOCAB.categories) {
    const group = document.createElement("optgroup");
    group.label = cat;
    VOCAB.behaviours
      .filter((b) => b.category === cat)
      .sort((a, b) => b.player_uses - a.player_uses || a.label.localeCompare(b.label))
      .forEach((b) => {
        const opt = document.createElement("option");
        opt.value = b.key;
        opt.textContent = b.label;
        group.appendChild(opt);
      });
    el.appendChild(group);
  }
  el.value = "damage";
}

/* Animations are named after spells people recognise. There are 700 of them,
 * so the list is alphabetical and searchable rather than ordered by some
 * internal notion of importance.
 *
 * The school it belongs to sorts the list rather than censoring it: a fire
 * animation on a holy spell is rarely what anyone meant, but it was worse to
 * hide 597 of 700 entries and leave the user hunting for Shadow Word: Pain on
 * a spell they had not set to Shadow yet. */
function fillVisuals() {
  const school = Number($("school").value);
  const chosen = state.visual_id || 0;
  const query = $("visual-search").value.trim().toLowerCase();
  const all = VOCAB.visuals || [];

  const matches = (v) => !query || v.label.toLowerCase().includes(query);
  const byName = (a, b) => a.label.localeCompare(b.label);
  // The one in use is always offered, or searching would silently unset it.
  const keep = (v) => matches(v) || v.value === chosen;
  const fitting = all.filter((v) => keep(v) && (!v.schools.length || v.schools.includes(school))).sort(byName);
  const rest = all.filter((v) => keep(v) && v.schools.length && !v.schools.includes(school)).sort(byName);

  const el = $("visual");
  el.innerHTML = "";
  const none = document.createElement("option");
  none.value = "0";
  none.textContent = "No animation";
  el.appendChild(none);
  addVisualGroup(el, schoolLabel(school) + " animations", fitting);
  addVisualGroup(el, fitting.length ? "Every other animation" : "All animations", rest);

  el.value = all.some((v) => v.value === chosen) ? String(chosen) : "0";
  state.visual_id = Number(el.value) || 0;
  openVisualList();
  el.onchange = () => {
    state.visual_id = Number(el.value) || 0;
    el.size = 1;
    onVisualChange();
    schedulePreview();
  };
  onVisualChange();
}

/* Typing in the filter opens the list underneath it, so what was matched is
 * visible without a second click.
 *
 * By growing the select rather than calling showPicker: opening the native
 * drop-down moves focus into it, so the next keystroke would go to the list's
 * own type-ahead instead of the filter box, and the filter would take one
 * letter and stop. A sized select is a list in the page, and the box above it
 * keeps the caret. */
const VISUAL_ROWS = 8;

function openVisualList() {
  const el = $("visual");
  const filtering = $("visual-search").value.trim() !== "";
  el.size = filtering ? Math.min(VISUAL_ROWS, Math.max(2, el.options.length)) : 1;
}

function addVisualGroup(el, label, list) {
  if (!list.length) return;
  const group = document.createElement("optgroup");
  group.label = `${label} (${list.length})`;
  for (const v of list) {
    const opt = document.createElement("option");
    opt.value = v.value;
    opt.textContent = v.label;
    group.appendChild(opt);
  }
  el.appendChild(group);
}

function schoolLabel(school) {
  const found = (VOCAB.schools || []).find((s) => s.value === school);
  return found ? found.label : "Matching";
}

function onVisualChange() {
  $("visual-hint").textContent =
    "The visual effect played when this ability is used.";
}

/* Being taught is the ordinary way to get an ability, so it is on by default and
 * decided for the user: the ability goes on the trainers the game already ships
 * for its class, or on one of ours when the game has none. The only thing worth
 * asking is what it costs. */
function goingRate(level) {
  const curve = VOCAB.price_curve || [];
  const found = curve.find((p) => p.level === level);
  return found || { cost: 0, money: "free" };
}

function priceCopper() {
  return (Number($("price-g").value) || 0) * 10000
    + (Number($("price-s").value) || 0) * 100
    + (Number($("price-c").value) || 0);
}

function setPrice(copper) {
  $("price-g").value = Math.floor(copper / 10000);
  $("price-s").value = Math.floor((copper % 10000) / 100);
  $("price-c").value = copper % 100;
}

/* The price follows the level until the user sets one of their own. */
function repriceIfUntouched() {
  const touched = ["price-g", "price-s", "price-c"].some((id) => $(id).dataset.touched);
  if (!touched) setPrice(goingRate(Number($("level").value) || 1).cost);
}

function refreshTaught() {
  $("trainer-row").hidden = !$("taught").checked;
}

/* A level is a fixed range, so it is a list rather than a box to type a wrong
 * number into. */
function fillLevels() {
  const el = $("level");
  const keep = el.value;
  el.innerHTML = "";
  for (let n = 1; n <= (VOCAB.max_level || 80); n++) {
    const opt = document.createElement("option");
    opt.value = String(n);
    opt.textContent = String(n);
    el.appendChild(opt);
  }
  el.value = keep || "1";
}

/* Each class has several trees and the busiest is only a default. Which one an
 * ability belongs to decides the tab it appears under. */
function fillTrees() {
  const chosen = (VOCAB.classes || []).find((c) => c.value === Number($("class").value));
  const trees = (chosen && chosen.trees) || [];
  const el = $("skill-line");
  const keep = Number(el.value);
  el.innerHTML = "";
  if (!trees.length) {
    const none = document.createElement("option");
    none.value = "0";
    none.textContent = "General";
    el.appendChild(none);
    el.disabled = true;
  } else {
    el.disabled = false;
    for (const t of trees) {
      const opt = document.createElement("option");
      opt.value = t.value;
      opt.textContent = t.label;
      el.appendChild(opt);
    }
    el.value = trees.some((t) => t.value === keep) ? String(keep) : String(trees[0].value);
  }
}

function fillTargets() {
  const el = $("target");
  el.innerHTML = "";
  for (const t of VOCAB.targets) {
    const opt = document.createElement("option");
    opt.value = t.key;
    opt.textContent = t.label;
    el.appendChild(opt);
  }
  el.value = "enemy";
  onTargetChange();
}

/* ------------------------------------------------------- behaviour blocks */

function behaviourDef(key) {
  return VOCAB.behaviours.find((b) => b.key === key);
}

function addBehaviour() {
  if (state.behaviours.length >= 3) return;
  const key = $("behaviour-add").value;
  const def = behaviourDef(key);
  const use = { key };
  for (const p of def.params) use[p.name] = p.default;
  state.behaviours.push(use);
  renderBehaviours();
  applyBehaviourDefaults(def);
  refreshVisibility();
  schedulePreview();
}

/* A newly added behaviour seeds the spell-level fields it cares about, so a
 * user who changes nothing still gets coherent numbers. Only ever fills in
 * fields the user has not already touched. */
function applyBehaviourDefaults(def) {
  if (def.needs_duration && !$("duration").dataset.touched) {
    $("duration").value = Math.round((def.duration_default || 15000) / 1000);
  }
  if (!$("radius").dataset.touched && def.radius_default) {
    $("radius").value = Math.round(def.radius_default);
  }
  if (!$("scaling").dataset.touched && def.scaling_default) {
    $("scaling").value = def.scaling_default;
  }
  // A behaviour that must be anchored to the ground forces the target that
  // makes that possible, rather than letting the user build something invalid.
  if (def.shape === "ground" && !def.targets.includes($("target").value)) {
    $("target").value = "ground";
    onTargetChange();
  }
}

function renderBehaviours() {
  const host = $("behaviours");
  host.innerHTML = "";
  state.behaviours.forEach((use, index) => {
    const def = behaviourDef(use.key);
    const card = document.createElement("div");
    card.className = "behaviour";

    const head = document.createElement("header");
    head.innerHTML = `<span class="cat">${def.category}</span><strong>${def.label}</strong>`;
    const drop = document.createElement("button");
    drop.className = "small ghost drop";
    drop.type = "button";
    drop.textContent = "Remove";
    drop.onclick = () => {
      state.behaviours.splice(index, 1);
      renderBehaviours();
      refreshVisibility();
      schedulePreview();
    };
    head.appendChild(drop);
    card.appendChild(head);

    const summary = document.createElement("div");
    summary.className = "summary";
    summary.textContent = def.summary;
    card.appendChild(summary);

    // Each effect slot has its own target in the game, so each behaviour can
    // aim somewhere different. Left alone it follows the spell.
    const aim = document.createElement("div");
    aim.className = "params aim";
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.setAttribute("for", `aim-${index}`);
    label.textContent = "Target";
    field.appendChild(label);
    const select = document.createElement("select");
    select.id = `aim-${index}`;
    const follow = document.createElement("option");
    follow.value = "";
    follow.textContent = `Same as the spell (${targetLabel($("target").value)})`;
    select.appendChild(follow);
    for (const key of def.targets) {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = targetLabel(key);
      select.appendChild(opt);
    }
    select.value = use.target || "";
    select.onchange = () => {
      use.target = select.value;
      renderBehaviours();
      schedulePreview();
    };
    field.appendChild(select);
    aim.appendChild(field);
    card.appendChild(aim);

    // Only the parameters this behaviour declares are ever drawn. A stun has
    // no amount, so a stun never shows an amount box.
    if (def.params.length) {
      const params = document.createElement("div");
      params.className = "params";
      const grid = document.createElement("div");
      grid.className = def.params.length > 1 ? "row" : "";
      for (const p of def.params) {
        grid.appendChild(paramField(p, use, index));
      }
      params.appendChild(grid);
      card.appendChild(params);
    }
    host.appendChild(card);
  });

  $("behaviour-hint").textContent = state.behaviours.length >= 3
    ? "That is all three slots used."
    : "An ability can have up to three effects.";
  $("behaviour-add-btn").disabled = state.behaviours.length >= 3;
}

function targetLabel(key) {
  const found = VOCAB.targets.find((t) => t.key === key);
  return found ? found.label : key;
}

function paramField(p, use, index) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const id = `p-${index}-${p.name}`;
  const label = document.createElement("label");
  label.setAttribute("for", id);
  label.textContent = p.label;
  wrap.appendChild(label);

  let input;
  if (p.options) {
    input = document.createElement("select");
    fillSelect(input, p.options, use[p.name]);
  } else {
    input = document.createElement("input");
    input.type = "number";
    input.step = p.name === "period" ? "0.5" : (p.share && !p.percent ? "0.1" : "1");
    input.min = "0";
    input.value = use[p.name];
  }
  input.id = id;
  input.oninput = () => {
    use[p.name] = Number(input.value);
    schedulePreview();
  };
  wrap.appendChild(input);

  if (p.help) {
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = p.help;
    wrap.appendChild(hint);
  }
  return wrap;
}

/* ------------------------------------------------------------- ranks */

/* 3.3.5a has no level scaling for player abilities. Ranks are how the game
 * makes one grow: each is its own spell with its own level, cost and numbers,
 * chained so only the highest known one shows. Every ability is rank 1, so an
 * ability nobody adds ranks to is exactly what it always was. */
function amountEffects() {
  return state.behaviours
    .map((use, index) => ({ use, index, def: behaviourDef(use.key) }))
    .filter((e) => e.def.has_amount);
}

function addRank() {
  const effects = amountEffects();
  const last = state.extra_ranks[state.extra_ranks.length - 1];
  // A new rank starts where the last one left off, on the assumption it is
  // being stepped up rather than started from nothing.
  state.extra_ranks.push(last
    ? { spell_level: last.spell_level, power_cost: last.power_cost,
        amounts: last.amounts.slice(), price_copper: -1 }
    : { spell_level: Number($("level").value) || 1,
        power_cost: Number($("cost").value) || 0,
        amounts: effects.map((e) => Number(e.use.amount) || 0),
        price_copper: -1 });
  renderRanks();
  schedulePreview();
}

function renderRanks() {
  const host = $("ranks");
  host.innerHTML = "";
  const effects = amountEffects();

  // Rank 1 is the ability itself. Its fields are the same fields as above, so
  // editing either edits the same thing.
  host.appendChild(rankRow("Rank 1", {
    level: { value: Number($("level").value) || 1, set: (v) => {
      $("level").value = v;
      repriceIfUntouched();
    } },
    cost: { value: Number($("cost").value) || 0, set: (v) => { $("cost").value = v; } },
    amounts: effects.map((e) => ({
      label: e.def.label,
      value: Number(e.use.amount) || 0,
      set: (v) => {
        e.use.amount = v;
        const box = document.getElementById(`p-${e.index}-amount`);
        if (box) box.value = v;
      },
    })),
  }));

  state.extra_ranks.forEach((values, index) => {
    host.appendChild(rankRow(`Rank ${index + 2}`, {
      level: { value: values.spell_level, set: (v) => { values.spell_level = v; } },
      cost: { value: values.power_cost, set: (v) => { values.power_cost = v; } },
      amounts: effects.map((e, slot) => ({
        label: e.def.label,
        value: values.amounts[slot] || 0,
        set: (v) => { values.amounts[slot] = v; },
      })),
    }, () => {
      state.extra_ranks.splice(index, 1);
      renderRanks();
      schedulePreview();
    }));
  });

}

function rankRow(label, fields, onremove) {
  const row = document.createElement("div");
  row.className = "rank-row";
  const who = document.createElement("div");
  who.className = "who";
  who.textContent = label;
  row.appendChild(who);
  const id = label.toLowerCase().replace(/\s+/g, "-");
  row.appendChild(rankField(`${id}-level`, "Level", fields.level.value, fields.level.set));
  row.appendChild(rankField(`${id}-cost`, powerName(), fields.cost.value, fields.cost.set));
  fields.amounts.forEach((amount, slot) => {
    row.appendChild(rankField(`${id}-amount-${slot}`, amount.label, amount.value,
                              amount.set));
  });
  const drop = document.createElement("button");
  drop.className = "small ghost drop";
  drop.type = "button";
  drop.textContent = "Remove";
  if (onremove) {
    drop.onclick = onremove;
  } else {
    // Rank 1 cannot be removed, but the row keeps its shape so the ranks line up.
    drop.disabled = true;
    drop.style.visibility = "hidden";
  }
  row.appendChild(drop);
  return row;
}

/* Writes the fields above into the Rank 1 row without rebuilding it. */
function syncFirstRank() {
  const level = document.getElementById("rank-1-level");
  const cost = document.getElementById("rank-1-cost");
  if (level) level.value = Number($("level").value) || 1;
  if (cost) cost.value = Number($("cost").value) || 0;
  const label = document.querySelector("#ranks .rank-row .field label[for='rank-1-cost']");
  if (label) label.textContent = powerName();
}

function rankField(id, label, value, onchange) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const el = document.createElement("label");
  el.setAttribute("for", id);
  el.textContent = label;
  const input = document.createElement("input");
  input.type = "number";
  input.min = "0";
  input.id = id;
  input.value = value;
  input.oninput = () => {
    onchange(Number(input.value) || 0);
      schedulePreview();
  };
  wrap.append(el, input);
  return wrap;
}

function powerName() {
  const found = VOCAB.powers.find((p) => p.value === Number($("power").value));
  return found ? found.label : "Cost";
}

/* ------------------------------------------------- progressive disclosure */

function refreshVisibility() {
  const any = state.behaviours.length > 0;
  $("step-cast").hidden = !any;
  // Scaling and spread only mean something when there is an amount to scale.
  const hasAmount = state.behaviours.some((u) => behaviourDef(u.key).has_amount);
  $("step-power").hidden = !hasAmount;
  if (hasAmount) refreshPowerHints();
  $("step-ranks").hidden = !any;
  if (any) renderRanks();

  const needsDuration = state.behaviours.some(
    (u) => behaviourDef(u.key).needs_duration);
  $("field-duration").hidden = !needsDuration;

  const target = VOCAB.targets.find((t) => t.key === $("target").value);
  $("field-radius").hidden = !(target && target.area);
  $("field-range").hidden = !!(target && target.family === "unit" && target.key === "self");
}

/* Both numbers are explained by what they do to this spell, not in the
 * abstract: the same 10% reads differently on a 500 damage tick. */
function refreshPowerHints() {
  const periodic = state.behaviours.some((u) => {
    const def = behaviourDef(u.key);
    return def.has_amount && def.shape === "periodic" || def.shape === "ground";
  });
  const scaling = Number($("scaling").value) || 0;
  const added = Math.round(scaling * 10);
  $("scaling-hint").textContent = scaling
    ? `A caster with 1000 spell power adds ${added}${periodic ? " to each tick" : ""}.`
    : "The caster's power makes no difference to this ability.";

  const first = state.behaviours.find((u) => behaviourDef(u.key).has_amount);
  const amount = first ? Number(first.amount) || 0 : 0;
  const spread = Number($("spread").value) || 0;
  const half = Math.round(amount * spread / 100);
  $("spread-hint").textContent = !spread
    ? "The ability does the same amount every time."
    : `${amount} lands anywhere between ${Math.max(1, amount - half)} and ${amount + half}.`;
}

function onTargetChange() {
  const target = VOCAB.targets.find((t) => t.key === $("target").value);
  $("target-hint").textContent = target ? target.summary : "";
  refreshVisibility();
}

["scaling", "spread"].forEach((id) => {
  document.addEventListener("input", (e) => {
    if (e.target && e.target.id === id) refreshPowerHints();
  });
});

["duration", "radius", "scaling"].forEach((id) => {
  document.addEventListener("input", (e) => {
    if (e.target && e.target.id === id) e.target.dataset.touched = "1";
  });
});

/* ------------------------------------------------------------- preview */

function collect() {
  const behaviours = state.behaviours.map((use) => {
    const def = behaviourDef(use.key);
    const out = { key: use.key };
    for (const p of def.params) {
      const value = Number(use[p.name]) || 0;
      if (p.name === "period") out.period_ms = Math.round(value * 1000);
      else if (p.name === "amount") out.amount = value;
      else if (p.name === "chain") out.chain = value;
      else if (p.name === "trigger") out.trigger = value;
      else if (p.share) out.share = p.percent ? value / 100 : value;
      else out.misc = value;
    }
    out.target = use.target || "";
    return out;
  });
  return {
    spell_id: state.spell_id,
    name: $("name").value,
    class_id: Number($("class").value) || 0,
    skill_line: Number($("skill-line").value) || 0,
    spell_level: Number($("level").value) || 1,
    description: $("description").value,
    icon_id: state.icon_id || 1,
    school: Number($("school").value),
    power_type: Number($("power").value),
    power_cost: Number($("cost").value) || 0,
    cast_time_ms: Math.round((Number($("cast").value) || 0) * 1000),
    cooldown_ms: Math.round((Number($("cooldown").value) || 0) * 1000),
    range_yards: Number($("range").value) || 0,
    target: $("target").value,
    radius_yards: Number($("radius").value) || 8,
    duration_ms: Math.round((Number($("duration").value) || 0) * 1000),
    extra_ranks: state.extra_ranks,
    power_scaling: Number($("scaling").value) || 0,
    spread_pct: Number($("spread").value) || 0,
    ignore_mitigation: $("ignore-mitigation").checked,
    visual_id: Number($("visual").value) || 0,
    behaviours,
  };
}

function schedulePreview() {
  setDirty(true);
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 120);
}

async function runPreview() {
  const data = await ask("/api/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ definition: collect() }),
  });
  if (data.error) { renderMessages([], [], data.error); return; }
  paintTooltip(data.tooltip);
  // Everything the save will run, in the order it runs: the spell, then the row
  // that says whose spell it is.
  $("sql").textContent = [data.sql, data.class_sql, data.scaling_sql]
    .filter(Boolean).join("\n\n");
  $("description").placeholder = data.suggested_description
    || "Left blank, a description is written from the effects you pick.";
  // An ability with no effects has nothing wrong with it yet. The Casting
  // section is not even on screen, so complaints about its defaults read as
  // mistakes the user made rather than fields they have not reached.
  if (state.behaviours.length) renderMessages(data.warnings, data.notes);
  else renderMessages([], []);
}

function paintTooltip(t) {
  $("tt-name").textContent = t.name;
  // With no behaviours there is nothing to describe, and a blank tooltip looks
  // like a failure rather than an empty spell.
  $("tooltip").classList.toggle("empty-spell", state.behaviours.length === 0);
  $("tt-cost").textContent = t.cost || "";
  $("tt-range").textContent = t.range || "";
  $("tt-cast").textContent = t.cast_time || "";
  $("tt-cd").textContent = t.cooldown || "";
  $("tt-desc").textContent = state.behaviours.length
    ? t.description
    : "Add an effect and this becomes the ability's description.";
  $("tt-rank").textContent = t.rank || "";
  $("tt-scaling").textContent = t.scaling || "";
  $("tt-level").textContent = t.level ? `Requires Level ${t.level}` : "";
  $("tt-sid").textContent = t.spell_id ? `Ability ${t.spell_id}` : "";
  paintIcon($("tt-icon"), iconName(t.icon_id));
}

function iconName(id) {
  const icon = VOCAB.icons.find((i) => i.id === id);
  return icon ? icon.name : "";
}

/* Falls back to the icon's name when no client is configured to read them
 * from. Nothing depends on the artwork loading. */
function paintIcon(host, name) {
  host.innerHTML = "";
  if (!name) return;
  const img = document.createElement("img");
  img.loading = "lazy";
  img.src = ICON_BASE + encodeURIComponent(name) + ".png";
  img.alt = name;
  img.onerror = () => { host.textContent = name.slice(0, 10); };
  host.appendChild(img);
}

function paintIconPreview() {
  paintIcon($("icon-preview"), iconName(state.icon_id));
}

/* Every request goes through here.
 *
 * When the server is gone - the window was closed, or Shut Down was used - a
 * bare fetch rejects with the browser's own words: "NetworkError when
 * attempting to fetch resource" in Firefox, "Failed to fetch" in Chrome.
 * Neither says the one thing worth knowing, which is that SpellWeaver is not
 * running. Every caller already handles `error`, so a failure is turned into
 * one rather than thrown, and the bar at the top of the page says what to do.
 */
/* Named after however this one was started, which the server tells us while it
 * is still there to ask. Until it has, the wording stays general rather than
 * pointing at a file that may not exist on that machine. */
let LAUNCHER = "";

function offlineWords() {
  return LAUNCHER
    ? `SpellWeaver is not running. Run ${LAUNCHER} to restart.`
    : "SpellWeaver is not running. Start it again to carry on.";
}

async function ask(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (err) {
    // Nothing answered. Telling a deliberate stop from a surprise: after Shut
    // Down the page already says so, and a second notice would argue with it.
    const deliberate = document.body.classList.contains("stopped");
    showOffline(!deliberate);
    return { error: deliberate ? "SpellWeaver has stopped." : offlineWords(),
             offline: true };
  }
  showOffline(false);
  try {
    return await res.json();
  } catch (err) {
    // Something answered, but not in JSON. That is a different fault from the
    // server being gone, and saying "not running" about it would send the user
    // off to fix the wrong thing.
    return { error: `The server answered ${res.status} with something this `
                    + "page could not read." };
  }
}

function showOffline(gone) {
  const bar = $("offline");
  if (!bar) return;
  if (gone) $("offline-words").textContent = offlineWords();
  bar.hidden = !gone;
}

function renderMessages(warnings, notes, error) {
  const host = $("msgs");
  host.innerHTML = "";
  const add = (cls, text) => addMsg(host, cls, text);
  if (error) add("err", error);
  (warnings || []).forEach((w) => add("warn", w));
  (notes || []).forEach((n) => add("note", n));
}

/* --------------------------------------------------------------- saving */

async function saveSpell() {
  // An ability with no name is one that was not finished. Saving it writes rows
  // to a live server, so the name is the one thing asked for up front.
  if (!$("name").value.trim()) {
    renderMessages([], [], "Give the ability a name before saving it.");
    $("name").focus();
    return;
  }
  // Warnings are hidden while the ability is empty, so this is where an ability
  // that does nothing has to be caught instead.
  if (!state.behaviours.length) {
    renderMessages([], [], "Add at least one effect before saving.");
    return;
  }
  const wasNew = !state.spell_id;
  const data = await ask("/api/spells", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ definition: collect(), training: collectTraining() }),
  });
  if (data.error) { renderMessages([], [], data.error); return; }
  state.spell_id = data.spell_id;
  await runPreview();
  setDirty(false);
  markEditing();
  renderLibrary();
  showSaved(data.spell_id, $("name").value, wasNew, data.training);
}

function collectTraining() {
  return { enabled: $("taught").checked, money_cost: priceCopper() };
}

/* Saving writes rows the server has already loaded and a client that has never
 * heard of them, so neither end shows the ability until something is restarted.
 * That is not guessable, so it is spelled out. */
function showSaved(id, name, wasNew, training) {
  $("saved-title").textContent = wasNew ? "Ability created" : "Ability updated";
  $("saved-what").textContent = `${name} is ability ${id}.`;
  const steps = [
    "<strong>Restart the worldserver.</strong> It only picks up new abilities "
      + "at startup.",
    "<strong>Apply the client patch</strong> and restart the client. Abilities "
      + "will not function correctly until this step is complete.",
  ];
  // Only worth a step if there is something to do: trainers the game already
  // has need nothing, one of ours has to be placed, and an ability nobody
  // teaches has to be handed out.
  if (training && training.spawn_command) {
    const who = (training.trainers[0] || {}).name || "the trainer";
    steps.push(`<strong>Spawn ${escapeHtml(who)}</strong> where you want it: `
      + `<code>${escapeHtml(training.spawn_command)}</code>.`);
  } else if (!training) {
    steps.push(`<strong>Learn it in game:</strong> <code>.learn ${id}</code>.`);
  }
  $("saved-steps").innerHTML = steps.map((t) => `<li>${t}</li>`).join("");
  $("saved-overlay").hidden = false;
}

function resetForm() {
  state = { spell_id: null, behaviours: [], icon_id: blankIcon(), extra_ranks: [] };
  $("name").value = "";
  $("description").value = "";
  $("icon-search").value = "";
  $("visual-search").value = "";
  $("cast").value = 0;
  $("cooldown").value = 0;
  $("cost").value = 0;
  $("range").value = 30;
  $("duration").value = 15;
  $("radius").value = 8;
  delete $("duration").dataset.touched;
  delete $("radius").dataset.touched;
  delete $("scaling").dataset.touched;
  $("scaling").value = 0;
  $("spread").value = 0;
  $("ignore-mitigation").checked = false;
  state.visual_id = 0;
  $("school").value = 2;
  $("power").value = 0;
  $("class").value = 0;
  $("level").value = 1;
  $("taught").checked = true;
  ["price-g", "price-s", "price-c"].forEach((id) => { delete $(id).dataset.touched; });
  repriceIfUntouched();
  refreshTaught();
  fillTrees();
  $("target").value = "enemy";
  fillVisuals();
  onTargetChange();
  renderBehaviours();
  renderIconGrid();
  paintIconPreview();
  refreshVisibility();
  schedulePreview();
  setDirty(false);
  markEditing();
}

/* ---------------------------------------------------------------- views */

function showBuilder() {
  setView("view-build", "nav-abilities");
  $("sidebar").hidden = false;
  renderLibrary();
}

/* One view and one nav button at a time; the sidebar belongs to the builder. */
function setView(view, nav) {
  for (const id of ["view-build", "view-patch", "view-settings"]) {
    $(id).hidden = id !== view;
  }
  for (const id of ["nav-abilities", "nav-patch", "nav-settings"]) {
    $(id).classList.toggle("ghost", id !== nav);
  }
  $("sidebar").hidden = view !== "view-build";
}

/* The rail lists what already exists, so building and browsing are one page.
 * Grouped by class, because a server that has been used for a while has more
 * abilities than fit in a rail and class is how people look for one. Groups
 * collapse; which ones are shut is remembered for the session only. */
const shutGroups = new Set();

async function renderLibrary() {
  const host = $("library-list");
  if (!host) return;
  const data = await ask("/api/spells");
  if (data.error) {
    host.innerHTML = "";
    addMsg(host, "err", data.error);
    return;
  }
  const spells = data.spells || [];
  $("library-count").textContent = spells.length ? String(spells.length) : "";
  host.innerHTML = "";
  if (!spells.length) {
    host.innerHTML = '<div class="empty">Nothing yet. The ability you build '
      + 'here will appear in this list.</div>';
    return;
  }
  for (const group of groupByClass(spells)) host.appendChild(groupBlock(group));
}

/* Classes in the vocabulary's own order, with the unrestricted ones last:
 * "Any class" is a catch-all rather than a class, so it reads oddly first. */
function groupByClass(spells) {
  const order = (VOCAB.classes || []).map((c) => c.value);
  const byClass = new Map();
  for (const sp of spells) {
    const key = sp.class_id || 0;
    if (!byClass.has(key)) byClass.set(key, []);
    byClass.get(key).push(sp);
  }
  const groups = [];
  for (const [key, list] of byClass) {
    const klass = (VOCAB.classes || []).find((c) => c.value === key);
    groups.push({
      key,
      label: klass && key ? klass.label : "Any class",
      rank: key ? order.indexOf(key) : Infinity,
      spells: list,
    });
  }
  groups.sort((a, b) => a.rank - b.rank || a.label.localeCompare(b.label));
  return groups;
}

function groupBlock(group) {
  const block = document.createElement("div");
  block.className = "group";
  const shut = shutGroups.has(group.key);

  const head = document.createElement("button");
  head.type = "button";
  head.className = "group-head";
  head.setAttribute("aria-expanded", String(!shut));

  // The caret is drawn in CSS: the triangle glyphs are not in every font, and
  // the ones that were missing came out as a dash.
  const caret = document.createElement("span");
  caret.className = "caret";
  head.appendChild(caret);

  const label = document.createElement("span");
  label.className = "label";
  label.textContent = group.label;
  head.appendChild(label);

  const count = document.createElement("span");
  count.className = "n";
  count.textContent = String(group.spells.length);
  head.appendChild(count);

  head.onclick = () => {
    if (shutGroups.has(group.key)) shutGroups.delete(group.key);
    else shutGroups.add(group.key);
    renderLibrary();
  };
  block.appendChild(head);

  const body = document.createElement("div");
  body.className = "group-body";
  body.hidden = shut;
  for (const sp of group.spells) body.appendChild(abilityCard(sp));
  block.appendChild(body);
  return block;
}

function abilityCard(sp) {
  const card = document.createElement("div");
  card.className = "ability-card";
  if (sp.spell_id === state.spell_id) card.classList.add("current");

  const art = document.createElement("span");
  art.className = "art";
  paintIcon(art, iconName(sp.icon_id));
  card.appendChild(art);

  const text = document.createElement("div");
  text.className = "text";
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = sp.name;
  text.appendChild(name);

  const klass = (VOCAB.classes || []).find((c) => c.value === sp.class_id);
  const tree = klass && (klass.trees || []).find((t) => t.value === sp.skill_line);
  const bits = [];
  if (tree) bits.push(tree.label);
  if (sp.ranks > 1) bits.push(`${sp.ranks} ranks`);
  if (bits.length) {
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = bits.join(" - ");
    text.appendChild(meta);
  }
  card.appendChild(text);

  const del = document.createElement("button");
  del.className = "small ghost del";
  del.textContent = "Delete";
  del.title = `Delete ${sp.name}`;
  del.onclick = async (ev) => {
    ev.stopPropagation();
    const what = sp.ranks > 1 ? `${sp.name} and its ${sp.ranks} ranks` : sp.name;
    if (!confirm(`Delete ${what}?`)) return;
    await ask(`/api/spells/${sp.spell_id}`, { method: "DELETE" });
    // Deleting the one on screen would otherwise leave the form editing a row
    // that is no longer there.
    if (sp.spell_id === state.spell_id) resetForm();
    renderLibrary();
  };
  card.appendChild(del);

  card.onclick = () => {
    if (state.dirty && !confirm(
      "This ability has unsaved changes. Open another one and lose them?")) return;
    location.hash = `#spell/${sp.spell_id}`;
    loadSpell(sp.spell_id);
  };
  return card;
}

function cell(text, className) {
  const td = document.createElement("td");
  if (className) td.className = className;
  td.textContent = text;
  return td;
}

/* ------------------------------------------------------------- setup */

function wireSetup() {
  $("nav-settings").onclick = () => { location.hash = "#settings"; showSetup(); };
  $("db-test").onclick = () => submitConnection(false);
  $("db-save").onclick = () => submitConnection(true);
  $("client-save").onclick = saveClient;
  $("shutdown").onclick = shutDown;
  $("restart-save").onclick = saveRestartCommand;
}

function fillSetup(state) {
  LAUNCHER = state.launcher || LAUNCHER;
  fillClientSetting(state.client || {});
  $("restart-command").value = (state.restart || {}).command || "";
  refreshRestartButton((state.restart || {}).set);
  const c = state.connection || {};
  $("db-host").value = c.host || "127.0.0.1";
  $("db-port").value = c.port || 3306;
  $("db-user").value = c.user || "root";
  $("db-world").value = c.world_db || "acore_world";
  $("db-characters").value = c.characters_db || "acore_characters";
  $("db-password").value = "";
  $("db-password").placeholder = c.has_password ? "Unchanged" : "";
  const where = {
    saved: "These details were saved here and are used as they are.",
    env: `Filled in from ${state.env ? state.env.path : "an AzerothCore .env"}. `
      + "Save to keep them, or change anything that is wrong.",
    environment: "Taken from environment variables.",
    none: "Nothing is set up yet. Enter the details for your database.",
  }[state.source] || "";
  $("db-source").textContent = where;
  if (!state.ready && state.problem) {
    $("db-result").innerHTML = "";
    $("db-result").appendChild(verdict(false, "Not connected", [state.problem]));
  }
}

async function submitConnection(save) {
  const body = {
    host: $("db-host").value.trim(),
    port: Number($("db-port").value) || 3306,
    user: $("db-user").value.trim(),
    password: $("db-password").value,
    world_db: $("db-world").value.trim(),
    characters_db: $("db-characters").value.trim(),
  };
  dbWorking(save ? "Saving..." : "Connecting...");
  let data;
  try {
    data = await ask(save ? "/api/settings/connection"
                          : "/api/settings/connection/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (data.error) data = { ok: false, message: data.error, details: [] };
  } finally {
    dbWorking("");
  }
  const host = $("db-result");
  host.innerHTML = "";
  host.appendChild(verdict(data.ok, data.message, data.details || []));
  // A saved connection changes what the rest of the page can do, so it starts
  // again rather than being patched up in place.
  if (data.saved && data.ready) setTimeout(() => location.reload(), 700);
}

function fillClientSetting(client) {
  $("client-dir").value = client.path || client.found || "";
  const host = $("client-result");
  host.innerHTML = "";
  if (!client.path && !client.found) return;
  const lines = client.details || [];
  if (client.remembered) {
    lines.push("Remembered from the last patch built. Save to keep it.");
  }
  host.appendChild(verdict(client.ok, client.message, lines));
}

async function saveClient() {
  clientWorking("Checking...");
  let data;
  try {
    data = await ask("/api/settings/client", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: $("client-dir").value.trim() }),
    });
    if (data.error) data = { ok: false, message: data.error, details: [] };
  } finally {
    clientWorking("");
  }
  const host = $("client-result");
  host.innerHTML = "";
  host.appendChild(verdict(data.ok, data.message, data.details || []));
}

function clientWorking(text) {
  $("client-working-text").textContent = text;
  $("client-working").hidden = !text;
  $("client-save").disabled = !!text;
}

function dbWorking(text) {
  $("db-working-text").textContent = text;
  $("db-working").hidden = !text;
  $("db-save").disabled = !!text;
  $("db-test").disabled = !!text;
}

/* Stopping the server from the page it serves. The reply arrives before the
 * socket closes, so the confirmation is real rather than assumed; after that
 * the page is inert and says so, because a dead page that looks alive is worse
 * than one that admits it. */
/* Restarting the worldserver, which is the last step of nearly every change.
 * The command itself lives in Settings; this only asks for it to be run, so
 * the page never carries one. */
function refreshRestartButton(isSet) {
  const button = $("patch-restart");
  if (!button) return;
  button.disabled = !isSet;
  button.title = isSet
    ? "Run the restart command from Settings"
    : "Set a restart command on the Settings page first";
}

async function restartServer() {
  const host = $("patch-msgs");
  const button = $("patch-restart");
  host.innerHTML = "";
  button.disabled = true;
  const was = button.textContent;
  button.textContent = "Restarting...";
  try {
    const data = await ask("/api/restart", { method: "POST" });
    if (data.error) addMsg(host, "err", data.error);
    else {
      addMsg(host, data.ok ? "note" : "err", data.message);
      // The server's own words, when it had any: that is where the reason is.
      if (data.output) addMsg(host, data.ok ? "note" : "err", data.output);
    }
  } finally {
    button.textContent = was;
  }
  button.disabled = false;
  refreshPatch();
}

async function saveRestartCommand() {
  const host = $("restart-result");
  host.innerHTML = "";
  const data = await ask("/api/settings/restart-command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command: $("restart-command").value }),
  });
  if (data.error) { addMsg(host, "err", data.error); return; }
  addMsg(host, "note", data.message);
  refreshRestartButton(!!data.command);
}

async function shutDown() {
  if (!confirm("Stop SpellWeaver? The page will stop working until you run it "
               + "again from a terminal.")) return;
  const host = $("shutdown-result");
  host.innerHTML = "";
  // Marked as a deliberate stop first, so that if the socket dies before the
  // reply arrives the failure is read as the stop working, not as a fault.
  document.body.classList.add("stopped");
  const data = await ask("/api/shutdown", { method: "POST" });
  addMsg(host, "note", data.message || data.error || "SpellWeaver has stopped.");
  $("shutdown").disabled = true;
}

function showSetup() {
  setView("view-settings", "nav-settings");
}

/* ------------------------------------------------------- client patch */

async function showPatch() {
  setView("view-patch", "nav-patch");
  await refreshPatch();
}

/* The buttons go quiet while they are working, and say so where they are rather
 * than in a block underneath that moves everything else down. */
function working(text) {
  $("patch-working-text").textContent = text;
  $("patch-working").hidden = !text;
  $("patch-build").disabled = !!text;
  $("patch-refresh").disabled = !!text;
}

async function refreshPatch() {
  working("Checking the client...");
  let data;
  try {
    data = await ask("/api/clientpatch");
  } finally {
    working("");
  }
  const host = $("patch-report");
  $("patch-msgs").innerHTML = "";
  host.innerHTML = "";

  // The verdict is about the client, never about a file we wrote somewhere else.
  const headline = data.ok
    ? "The client is up to date"
    : (data.installed ? "The client's patch is behind"
      : (data.client ? "No patch in this client" : "No client set"));
  const problems = (data.problems || []).slice();
  if (!data.client) problems.push("Set the client folder on the Settings tab.");
  host.appendChild(verdict(data.ok, headline, problems));

  const rows = [["Client", data.client || "not set"]];
  if (data.installed) {
    rows.push(["Patch", data.patch]);
    rows.push(["Written", new Date(data.written_at * 1000).toLocaleString()]);
    rows.push(["Size", `${(data.size / 1e6).toFixed(1)} MB`]);
    // Names and ids are two different questions, so they get a row each rather
    // than the same list printed twice.
    const inIt = data.spells || [];
    rows.push(["Abilities", inIt.map((a) => a.name || `(${a.id})`).join(", ") || "none"]);
    rows.push(["Ids", inIt.map((a) => a.id).join(", ") || "none"]);
  }
  const table = document.createElement("table");
  table.className = "spells";
  table.innerHTML = "<tbody>" + rows.map(
    ([k, v]) => `<tr><td class="id">${escapeHtml(k)}</td>`
      + `<td>${escapeHtml(String(v))}</td></tr>`).join("") + "</tbody>";
  host.appendChild(table);
}

function verdict(ok, headline, problems) {
  const box = document.createElement("div");
  box.className = `verdict ${ok ? "good" : "bad"}`;
  const mark = document.createElement("span");
  mark.className = "mark";
  mark.textContent = ok ? "\u2713" : "\u2717";
  const body = document.createElement("div");
  const line = document.createElement("div");
  line.className = "headline";
  line.textContent = headline;
  body.appendChild(line);
  if (problems.length) {
    const list = document.createElement("ul");
    for (const p of problems) {
      const li = document.createElement("li");
      li.textContent = p;
      list.appendChild(li);
    }
    body.appendChild(list);
  }
  box.append(mark, body);
  return box;
}

async function buildPatch() {
  const msgs = $("patch-msgs");
  msgs.innerHTML = "";
  working("Build in progress...");
  let data;
  try {
    data = await ask("/api/clientpatch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
  } finally {
    working("");
  }
  // A write that could not happen is the thing most worth saying plainly. A
  // write that worked speaks for itself: the verdict below turns green and the
  // timestamp moves.
  if (data.error) addMsg(msgs, "err", data.error);
  await refreshPatch();
}

/* Each kind of message carries its own mark. Colour alone said which was
 * which, which is nothing to anyone reading in greyscale or not looking for it.
 * Drawn rather than typed: the glyphs are not in every font, and the one that
 * was missing came out as a dash. */
const MSG_MARKS = {
  note: '<circle cx="8" cy="8" r="6.4"/><path d="M8 7.4v4.1"/>'
        + '<path d="M8 4.6v.1"/>',
  warn: '<path d="M8 1.9 15 14.2H1z"/><path d="M8 6.2v3.6"/><path d="M8 11.8v.1"/>',
  err: '<circle cx="8" cy="8" r="6.4"/><path d="m5.6 5.6 4.8 4.8"/>'
       + '<path d="m10.4 5.6-4.8 4.8"/>',
  ok: '<circle cx="8" cy="8" r="6.4"/><path d="m5.2 8.2 2 2 3.6-4.2"/>',
};

function addMsg(host, cls, text) {
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  const marks = MSG_MARKS[cls] || MSG_MARKS.note;
  const mark = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  mark.setAttribute("class", "mark");
  mark.setAttribute("viewBox", "0 0 16 16");
  mark.setAttribute("aria-hidden", "true");
  mark.setAttribute("focusable", "false");
  mark.innerHTML = marks;
  div.appendChild(mark);
  const words = document.createElement("span");
  words.textContent = text;
  div.appendChild(words);
  host.appendChild(div);
}

async function loadSpell(id) {
  const data = await ask(`/api/spells/${id}`);
  if (data.error) return;
  const d = data.definition;
  state.spell_id = d.spell_id;
  state.behaviours = d.behaviours.map((b) => {
    const def = behaviourDef(b.key);
    const use = { key: b.key };
    for (const p of def.params) {
      if (p.name === "period") use.period = (b.period_ms || 0) / 1000;
      else if (p.name === "amount") use.amount = b.amount;
      else if (p.name === "chain") use.chain = b.chain;
      else if (p.name === "trigger") use.trigger = b.trigger;
      else if (p.share) use[p.name] = p.percent ? (b.share || 0) * 100 : (b.share || 0);
      else use[p.name] = b.misc;
    }
    use.target = b.target || "";
    return use;
  });
  $("name").value = d.name;
  $("description").value = d.description || "";
  state.extra_ranks = (d.extra_ranks || []).map((r) => ({
    spell_level: r.spell_level, power_cost: r.power_cost,
    amounts: (r.amounts || []).slice(), price_copper: r.price_copper,
  }));
  state.icon_id = d.icon_id;
  // Show the spell's own icon rather than the first sixty alphabetically, so
  // the current choice is visible and highlighted when reopening a spell.
  $("icon-search").value = iconName(d.icon_id) || "";
  renderIconGrid();
  const training = data.training;
  $("taught").checked = !!training;
  if (training) {
    setPrice(training.money_cost);
    ["price-g", "price-s", "price-c"].forEach((id) => { $(id).dataset.touched = "1"; });
  }
  $("class").value = d.class_id || 0;
  $("skill-line").value = d.skill_line || 0;
  fillTrees();
  if (d.skill_line) $("skill-line").value = d.skill_line;
  $("level").value = d.spell_level || 1;
  refreshTaught();
  $("school").value = d.school;
  $("power").value = d.power_type;
  $("cost").value = d.power_cost;
  $("cast").value = (d.cast_time_ms || 0) / 1000;
  $("cooldown").value = (d.cooldown_ms || 0) / 1000;
  $("range").value = d.range_yards;
  $("target").value = d.target;
  $("radius").value = d.radius_yards;
  $("duration").value = (d.duration_ms || 0) / 1000;
  $("scaling").value = d.power_scaling || 0;
  $("spread").value = d.spread_pct || 0;
  $("ignore-mitigation").checked = !!d.ignore_mitigation;
  state.visual_id = d.visual_id || 0;
  fillVisuals();
  $("duration").dataset.touched = "1";
  $("scaling").dataset.touched = "1";
  $("radius").dataset.touched = "1";
  onTargetChange();
  renderBehaviours();
  paintIconPreview();
  refreshVisibility();
  showBuilder();
  schedulePreview();
  if (location.hash !== `#spell/${id}`) location.hash = `#spell/${id}`;
  setDirty(false);
  markEditing();
  renderLibrary();
}

/* The page says which of the two things it is doing. */
function markEditing() {
  const editing = !!state.spell_id;
  $("save").textContent = editing ? "Save Changes" : "Save Ability";
  // Reverting an ability that exists puts back what is stored; on one that does
  // not exist yet there is nothing to go back to, so it simply clears.
  $("revert").textContent = editing ? "Revert Changes" : "Discard";
}

/* Saving is only worth offering once there is something to save. */
function setDirty(value) {
  state.dirty = value;
  $("edit-actions").hidden = !value;
}

function revertChanges() {
  if (state.spell_id) loadSpell(state.spell_id);
  else resetForm();
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

boot();
