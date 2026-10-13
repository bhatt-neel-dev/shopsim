/* ShopSim storefront: a small single-page shop, instrumented with the Motadata RUM SDK.
 *
 * Every call to /api/... carries a W3C traceparent from the SDK, so the RUM resource and the
 * backend trace share one trace id. Frontend faults (JS errors, slow renders, layout shifts,
 * broken images, slow LCP) come from the control panel via /internal/frontend.
 */
"use strict";

const CFG = window.SHOPSIM || {};
const RUM = window.MD_RUM;
const rumOn = !!(CFG.rum && RUM);
if (rumOn) {
  RUM.init({
    applicationId: CFG.rum.applicationId,
    clientToken: CFG.rum.clientToken,
    proxy: location.origin + "/rum",
    service: CFG.rum.service,
    env: CFG.rum.env,
    version: CFG.rum.version,
    sessionSampleRate: 100,
    sessionReplaySampleRate: 0,
    traceSampleRate: 100,
    trackUserInteractions: true,
    trackResources: true,
    trackLongTasks: true,
    defaultPrivacyLevel: "allow",
    allowedTracingUrls: [{match: location.origin + "/api/", propagatorTypes: ["tracecontext"]}],
  });
}

const $ = (s, r = document) => r.querySelector(s);
const app = $("#app");
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const money = v => `$${Number(v || 0).toFixed(2)}`;
const slug = s => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40);

// ---- user (fake sign-in, kept in localStorage) -------------------------------------------------
const FIRST = ["Asha", "Liam", "Mei", "Omar", "Sofia", "Kenji", "Priya", "Lucas", "Amara", "Noah", "Ines", "Ravi"];
const LAST = ["Rao", "Smith", "Chen", "Haddad", "Garcia", "Tanaka", "Patel", "Silva", "Okafor", "Müller", "Dubois", "Kumar"];
const COUNTRIES = [["US", "Austin"], ["IN", "Pune"], ["GB", "Leeds"], ["DE", "Berlin"], ["JP", "Osaka"], ["BR", "Recife"], ["FR", "Lyon"], ["AU", "Perth"]];
function storeGet(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } }
function storeSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } }
function newUser(name) {
  const pick = a => a[Math.floor(Math.random() * a.length)];
  const full = name || `${pick(FIRST)} ${pick(LAST)}`;
  const [country, city] = pick(COUNTRIES);
  const id = `CUS-${100000 + Math.floor(Math.random() * 900000)}`;
  return {id, name: full, email: `${slug(full).replace(/-/g, ".")}${Math.floor(Math.random() * 90) + 10}@shopsim.dev`, country, city};
}
let user = storeGet("shopsim_user");
function signIn(u) {
  user = u; storeSet("shopsim_user", u);
  if (rumOn) RUM.setUser({id: u.id, name: u.name, email: u.email});
  $("#account-link").textContent = u.name.split(" ")[0];
}
if (user) signIn(user);

// ---- API -----------------------------------------------------------------------------------------
class ApiError extends Error {
  constructor(status, message, path) { super(message); this.name = "ApiError"; this.status = status; this.path = path; }
}
async function api(method, path, body) {
  const res = await fetch(path, {method, credentials: "same-origin",
    headers: body !== undefined ? {"content-type": "application/json"} : {},
    body: body !== undefined ? JSON.stringify(body) : undefined});
  let data = null;
  try { data = await res.json(); } catch { /* empty or not JSON */ }
  if (!res.ok) throw new ApiError(res.status, (data && (data.message || data.detail)) || `HTTP ${res.status}`, path);
  return data;
}
function notice(html, kind = "err") {
  $("#notice").innerHTML = html ? `<div class="alert ${kind}">${html}</div>` : "";
}
function reportError(err, context) {
  notice(`Sorry, something went wrong${err.status ? ` (${err.status})` : ""}: ${esc(err.message)}`);
  if (rumOn) RUM.addError(err, context || {});
}

// ---- frontend faults from the control panel ------------------------------------------------------
let FAULTS = {}, faultsAt = 0;
async function loadFaults() {
  if (Date.now() - faultsAt < 5000) return FAULTS;
  try { FAULTS = await (await fetch("/internal/frontend", {cache: "no-store"})).json() || {}; faultsAt = Date.now(); }
  catch { /* keep the last ones */ }
  return FAULTS;
}
function pickWeighted(weights) {
  const entries = Object.entries(weights || {}).filter(([, w]) => w > 0);
  let roll = Math.random() * entries.reduce((a, [, w]) => a + w, 0);
  for (const [k, w] of entries) { if ((roll -= w) <= 0) return k; }
  return entries.length ? entries[0][0] : null;
}
function imgUrl(p) {
  if (Math.random() < (FAULTS.broken_asset_rate || 0)) return `/img/missing-${p.id}.png`;
  const delay = FAULTS.lcp_delay_ms > 0 ? `?delay=${Math.round(FAULTS.lcp_delay_ms)}` : "";
  return `/img/p-${p.id}-${slug(p.name)}.svg${delay}`;
}
function blockMainThread(ms) {
  const end = performance.now() + ms;
  let x = 0;
  while (performance.now() < end) x += Math.sqrt(x + 1);
  return x;
}
class CartSyncError extends Error { constructor(m) { super(m); this.name = "CartSyncError"; } }
class CheckoutError extends Error { constructor(m) { super(m); this.name = "CheckoutError"; } }
const JS_ERRORS = {
  type_error() { const promo = window.__shopPromo; return promo.discount.percent; },
  reference_error() { return trackRecentlyViewed(location.pathname); }, // eslint-disable-line no-undef
  promise_rejection() {
    fetch(`/api/catalog/recommendations?for=${encodeURIComponent(location.pathname)}`).then(r => {
      if (!r.ok) throw new Error(`Recommendations failed: HTTP ${r.status}`);
    });
  },
  custom() {
    const local = Math.floor(Math.random() * 9) + 1;
    if (Math.random() < 0.5) throw new CartSyncError(`Cart sync failed: version conflict (local ${local}, server ${local + 2})`);
    throw new CheckoutError(`Shipping quote missing for postcode ${10000 + Math.floor(Math.random() * 89999)}`);
  },
  console_error() { console.error("PriceWidget: currency missing for SKU", `SKU-${Math.floor(Math.random() * 1e6)}`); },
};
function applyPageFaults() {
  const f = FAULTS;
  if (f.slow_render_ms > 0 && Math.random() < (f.slow_render_rate || 0)) blockMainThread(f.slow_render_ms);
  if (Math.random() < (f.layout_shift_rate || 0)) {
    setTimeout(() => {
      const banner = document.createElement("div");
      banner.className = "promo";
      banner.textContent = "Flash sale: 20% off everything for the next hour!";
      app.prepend(banner);
    }, 300 + Math.random() * 900);
  }
  if (Math.random() < (f.js_error_rate || 0)) {
    const kind = pickWeighted(f.js_error_types);
    if (kind && JS_ERRORS[kind]) setTimeout(() => JS_ERRORS[kind](), 200 + Math.random() * 1500);
  }
}

// ---- cart badge & categories -----------------------------------------------------------------
function setCartCount(n) { $("#cart-count").textContent = n || 0; }
async function refreshCart() {
  try { const cart = await api("GET", "/api/cart"); setCartCount(cart.count); return cart; }
  catch (err) { return null; }
}
async function loadCategories() {
  try {
    const cats = await api("GET", "/api/catalog/categories");
    $("#cats").innerHTML = cats.items.map(c => `<a href="/category/${encodeURIComponent(c.category)}" data-link data-testid="cat-link">${esc(c.category)}</a>`).join("");
  } catch { /* the page still works without the bar */ }
}

// ---- pages ---------------------------------------------------------------------------------------
function productCard(p) {
  return `<a class="card" href="/product/${p.id}" data-link data-testid="product-card">
    <img src="${imgUrl(p)}" alt="${esc(p.name)}" loading="lazy" width="640" height="480">
    <div class="body"><span class="name">${esc(p.name)}</span><span class="muted">${esc(p.category)} · ★ ${p.rating ?? "-"}</span>
    <span class="price">${money(p.price)}</span></div></a>`;
}
async function home() {
  const delay = FAULTS.lcp_delay_ms > 0 ? `?delay=${Math.round(FAULTS.lcp_delay_ms)}` : "";
  const products = await api("GET", "/api/catalog/products?limit=12");
  app.innerHTML = `<img class="hero" src="/img/hero-autumn-sale.svg${delay}" alt="Autumn sale" fetchpriority="high">
    <h1>Picked for you</h1><div class="grid">${products.items.map(productCard).join("")}</div>`;
}
async function category(name) {
  name = decodeURIComponent(name);
  const products = await api("GET", `/api/catalog/products?category=${encodeURIComponent(name)}&limit=24`);
  app.innerHTML = `<h1>${esc(name)}</h1><p class="muted">${products.count} products</p>
    <div class="grid">${products.items.map(productCard).join("") || "<p>Nothing here yet.</p>"}</div>`;
}
async function search() {
  const q = new URLSearchParams(location.search).get("q") || "";
  const products = await api("GET", `/api/catalog/products?q=${encodeURIComponent(q)}&limit=24`);
  app.innerHTML = `<h1>Results for “${esc(q)}”</h1><p class="muted">${products.count} found</p>
    <div class="grid">${products.items.map(productCard).join("") || "<p>No matches. Try another word.</p>"}</div>`;
}
async function product(id) {
  const p = await api("GET", `/api/catalog/products/${id}`);
  const out = p.stock <= 0;
  app.innerHTML = `<div class="detail"><img src="${imgUrl(p)}" alt="${esc(p.name)}" fetchpriority="high" width="640" height="480">
    <div><span class="badge">${esc(p.category)}</span><h1>${esc(p.name)}</h1>
      <p class="muted">by ${esc(p.brand)} · SKU ${esc(p.sku)} · ★ ${p.rating}</p>
      <p class="price" style="font-size:24px">${money(p.price)}</p>
      <p class="muted">${out ? "Out of stock" : `${p.stock} in stock · ships in 1-3 days`}</p>
      <div class="actions"><input class="qty" type="number" min="1" max="20" value="1" id="qty" aria-label="Quantity">
        <button class="btn" data-testid="add-to-cart" id="add" ${out ? "disabled" : ""}>Add to cart</button>
        <button class="btn ghost" data-testid="coupon" id="coupon">Apply coupon</button></div>
      <div id="coupon-msg" class="muted"></div></div></div>`;
  $("#add").addEventListener("click", async e => {
    e.target.disabled = true;
    try {
      const cart = await api("POST", "/api/cart/items", {product_id: Number(id), qty: Number($("#qty").value) || 1});
      setCartCount(cart.count);
      notice(`Added to cart · <a href="/cart" data-link data-testid="cart-link">View cart (${cart.count})</a>`, "ok");
    } catch (err) { reportError(err, {action: "add_to_cart", product_id: id}); }
    finally { e.target.disabled = false; }
  });
  // Deliberately unresponsive for a while: the classic rage-click target.
  $("#coupon").addEventListener("click", () => {
    clearTimeout(window.__couponTimer);
    window.__couponTimer = setTimeout(() => { $("#coupon-msg").textContent = "Coupon service is busy. Try again later."; }, 2500);
  });
}
async function cart() {
  const c = await api("GET", "/api/cart");
  setCartCount(c.count);
  if (!c.items.length) { app.innerHTML = `<h1>Your cart</h1><div class="panel">Your cart is empty. <a href="/" data-link>Keep shopping</a></div>`; return; }
  app.innerHTML = `<h1>Your cart</h1><div class="panel"><table><thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Price</th><th></th></tr></thead>
    <tbody>${c.items.map(i => `<tr><td><a href="/product/${i.product_id}" data-link>${esc(i.name)}</a><div class="muted">${esc(i.sku)}</div></td>
      <td class="num">${i.qty}</td><td class="num">${money(i.price * i.qty)}</td>
      <td class="num"><button class="btn ghost" data-testid="remove-item" data-remove="${i.product_id}">Remove</button></td></tr>`).join("")}</tbody>
    <tfoot><tr><th>Total</th><th></th><th class="num">${money(c.total)}</th><th></th></tr></tfoot></table>
    <div class="row" style="margin-top:14px;justify-content:flex-end"><a class="btn" href="/checkout" data-link data-testid="checkout">Checkout</a></div></div>`;
  app.querySelectorAll("[data-remove]").forEach(b => b.addEventListener("click", async () => {
    try { await api("DELETE", `/api/cart/items/${b.dataset.remove}`); render(); } catch (err) { reportError(err, {action: "remove_item"}); }
  }));
}
async function checkout() {
  const c = await api("GET", "/api/cart");
  if (!c.items.length) { app.innerHTML = `<h1>Checkout</h1><div class="panel">Your cart is empty.</div>`; return; }
  const u = user || newUser();
  app.innerHTML = `<h1>Checkout</h1><form class="panel" id="co" data-testid="checkout-form">
    <p class="muted">${c.count} item(s) · total <b>${money(c.total)}</b> + shipping and tax</p>
    <div class="form">
      <label>Full name<input name="name" required value="${esc(u.name)}"></label>
      <label>Email<input name="email" type="email" required value="${esc(u.email)}"></label>
      <label>City<input name="city" value="${esc(u.city)}"></label>
      <label>Country<select name="country">${COUNTRIES.map(([cc]) => `<option ${cc === u.country ? "selected" : ""}>${cc}</option>`).join("")}</select></label>
      <label>Payment<select name="payment_method"><option value="card">Card</option><option value="wallet">Wallet</option><option value="paypal">PayPal</option></select></label>
      <label>Card<select name="brand"><option>visa</option><option>mastercard</option><option>amex</option><option>rupay</option></select></label>
      <label>Card last 4<input name="last4" pattern="\\d{4}" value="${String(1000 + Math.floor(Math.random() * 8999))}"></label>
      <label>Shipping<select name="shipping_method"><option value="standard">Standard</option><option value="express">Express</option><option value="same_day">Same day</option></select></label>
      <label>Coupon<input name="coupon" placeholder="optional"></label>
    </div>
    <div class="row" style="margin-top:16px;justify-content:flex-end"><button class="btn" type="submit" data-testid="place-order">Place order</button></div></form>`;
  $("#co").addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target, btn = f.querySelector("button[type=submit]");
    btn.disabled = true; btn.textContent = "Placing order…";
    const customer = {...u, name: f.name.value, email: f.email.value, city: f.city.value, country: f.country.value};
    if (!user) signIn(customer);
    try {
      const order = await api("POST", "/api/checkout", {customer, payment_method: f.payment_method.value,
        card: f.payment_method.value === "card" ? {brand: f.brand.value, last4: f.last4.value || "4242"} : null,
        shipping_method: f.shipping_method.value, coupon: f.coupon.value || null});
      setCartCount(0);
      if (rumOn) RUM.addAction("order_placed", {order_number: order.order_number, total: order.total});
      navigate(`/orders/${order.id}?placed=1`);
    } catch (err) {
      reportError(err, {action: "checkout"});
      btn.disabled = false; btn.textContent = "Try again";
    }
  });
}
async function orders() {
  if (!user) { app.innerHTML = `<h1>Your orders</h1><div class="panel"><a href="/login" data-link data-testid="login-link">Sign in</a> to see your orders.</div>`; return; }
  const list = await api("GET", `/api/orders/orders?customer_id=${encodeURIComponent(user.id)}&limit=10`);
  app.innerHTML = `<h1>Your orders</h1><div class="panel">${list.items.length ? `<table><thead><tr><th>Order</th><th>Status</th><th class="num">Total</th></tr></thead>
    <tbody>${list.items.map(o => `<tr><td><a href="/orders/${o.id}" data-link data-testid="order-row">${esc(o.order_number)}</a><div class="muted">${esc(String(o.created_at).slice(0, 16).replace("T", " "))}</div></td>
      <td><span class="badge">${esc(o.status)}</span></td><td class="num">${money(o.total)}</td></tr>`).join("")}</tbody></table>`
    : "No orders yet."}</div>`;
}
async function orderDetail(id) {
  const o = await api("GET", `/api/orders/orders/${id}`);
  const placed = new URLSearchParams(location.search).get("placed");
  app.innerHTML = `${placed ? `<div class="alert ok" data-testid="order-confirmation">Thank you! Order ${esc(o.order_number)} is confirmed.</div>` : ""}
    <h1>Order ${esc(o.order_number)}</h1><div class="panel"><p><span class="badge">${esc(o.status)}</span> · ${esc(o.payment_method)} · ${esc(o.shipping_method)}</p>
    <table><tbody>${(o.items || []).map(i => `<tr><td>${esc(i.name)}</td><td class="num">${i.qty} × ${money(i.price)}</td></tr>`).join("")}</tbody>
    <tfoot><tr><th>Total</th><th class="num">${money(o.total)}</th></tr></tfoot></table>
    <p style="margin-top:14px"><a href="/orders" data-link data-testid="orders-link">All orders</a></p></div>`;
}
async function account() {
  if (!user) { navigate("/login"); return; }
  app.innerHTML = `<h1>Hello, ${esc(user.name.split(" ")[0])}</h1><div class="panel"><p>${esc(user.email)} · ${esc(user.city)}, ${esc(user.country)}</p>
    <div class="row"><a class="btn" href="/orders" data-link data-testid="orders-link">Your orders</a>
    <button class="btn ghost" id="signout" data-testid="sign-out">Sign out</button></div></div>`;
  $("#signout").addEventListener("click", () => {
    user = null; localStorage.removeItem("shopsim_user"); if (rumOn) RUM.clearUser();
    $("#account-link").textContent = "Sign in"; navigate("/");
  });
}
async function login() {
  const u = newUser();
  app.innerHTML = `<h1>Sign in</h1><form class="panel" id="login" data-testid="login-form"><div class="form">
    <label>Name<input name="name" required value="${esc(u.name)}"></label><label>Email<input name="email" type="email" required value="${esc(u.email)}"></label>
    <label>Password<input name="password" type="password" value="hunter2hunter2"></label></div>
    <div class="row" style="margin-top:16px;justify-content:flex-end"><button class="btn" type="submit" data-testid="login-submit">Sign in</button></div></form>`;
  $("#login").addEventListener("submit", e => {
    e.preventDefault();
    signIn({...u, name: e.target.name.value, email: e.target.email.value});
    navigate("/account");
  });
}

// ---- router --------------------------------------------------------------------------------------
const ROUTES = [
  [/^\/$/, home], [/^\/category\/(.+)$/, category], [/^\/product\/(\d+)$/, product], [/^\/search$/, search],
  [/^\/cart$/, cart], [/^\/checkout$/, checkout], [/^\/orders$/, orders], [/^\/orders\/(\d+)$/, orderDetail],
  [/^\/account$/, account], [/^\/login$/, login],
];
function navigate(path) { history.pushState({}, "", path); render(); }
async function render() {
  const path = location.pathname;
  notice("");
  app.innerHTML = `<div class="skeleton">Loading…</div>`;
  await loadFaults();
  for (const [re, page] of ROUTES) {
    const m = path.match(re);
    if (!m) continue;
    try { await page(...m.slice(1)); }
    catch (err) {
      app.innerHTML = `<h1>We couldn't load this page</h1><p class="muted">Please try again in a moment.</p>`;
      reportError(err, {page: path});
    }
    applyPageFaults();
    window.scrollTo(0, 0);
    return;
  }
  app.innerHTML = `<h1>Page not found</h1><p><a href="/" data-link>Back to the shop</a></p>`;
}
document.addEventListener("click", e => {
  const a = e.target.closest("a[data-link]");
  if (!a || e.metaKey || e.ctrlKey) return;
  e.preventDefault();
  navigate(a.getAttribute("href"));
});
document.querySelector("[data-testid=search-form]").addEventListener("submit", e => {
  e.preventDefault();
  navigate(`/search?q=${encodeURIComponent(e.target.q.value.trim())}`);
});
window.addEventListener("popstate", render);
loadCategories();
refreshCart();
render();
