(function (global) {
  'use strict';

  function serviceDate(epoch) {
    const date = new Date(Number(epoch || 0) * 1000);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString('sk-SK', {timeZone:'Europe/Bratislava'});
  }

  function subscriptionAmount(cents) {
    if (typeof cents !== 'number' || !Number.isInteger(cents) || cents < 0) return '—';
    return (cents / 100).toLocaleString('sk-SK', {
      minimumFractionDigits: 0, maximumFractionDigits: 2
    }) + ' €';
  }

  function subscriptionPortalHelp() {
    return 'Správa predplatného je dočasne nedostupná. Predplatné zostáva nezmenené. Skús to znova alebo zavolaj na +421 917 347 009, prípadne napíš na pumaragency@gmail.com.';
  }

  function subscriptionManagementHtml(data, state) {
    if (state && state.error) return `<section class="card subscription-card"><span class="eyebrow eyebrow--danger">Premium · predplatné</span><h2>Stav sa nepodarilo načítať</h2><p class="muted" role="status" aria-live="polite">${subscriptionPortalHelp()}</p></section>`;
    data = data && typeof data === 'object' ? data : {};
    const status = typeof data.status === 'string' ? data.status : '';
    const hasAccess = data.subscription_has_access === true;
    const canManage = data.can_manage === true;
    const review = data.needs_review === true;
    let title = 'Tvoje predplatné';
    let description = '';
    let facts = '';
    const amount = subscriptionAmount(data.next_amount_cents);
    const renewal = Number(data.renews_at) > 0 ? serviceDate(data.renews_at) : '';
    const cutoff = Number(data.access_until) > 0 ? serviceDate(data.access_until) : '';
    const fact = (label, value) => value
      ? `<div><dt>${label}</dt><dd>${value}</dd></div>` : '';

    if (review) {
      title = 'Stav predplatného overujeme';
      description = hasAccess
        ? 'Premium je podľa posledného potvrdeného stavu aktívne do uvedeného dátumu.'
        : 'Premium teraz nie je aktívne. Potvrdený stav preverujeme.';
      facts = fact(hasAccess ? 'Potvrdený prístup do' : 'Prístup skončil', cutoff);
    } else if (status === 'active' && hasAccess) {
      title = 'Aktívne';
      description = 'Premium je aktívne. Predplatné sa obnovuje každý rok.';
      facts = fact('Ďalšia ročná platba', amount === '—' ? '' : amount)
        + fact('Dátum obnovy', renewal);
    } else if (status === 'cancelled') {
      if (hasAccess) {
        title = 'Obnovenie je zrušené';
        description = 'Ďalšia platba nevznikne. Premium je aktívne do konca zaplateného obdobia.';
        facts = fact('Prístup do', cutoff);
      } else {
        title = 'Predplatné skončilo';
        description = 'Zaplatené obdobie sa skončilo.';
        facts = fact('Prístup skončil', cutoff);
      }
    } else if (status === 'past_due') {
      title = hasAccess ? 'Platba sa rieši' : 'Premium je pozastavené';
      description = hasAccess
        ? 'Platba sa rieši u poskytovateľa. Premium je podľa potvrdeného stavu aktívne.'
        : 'Platba sa rieši u poskytovateľa. Premium teraz nie je aktívne.';
      facts = fact('Ročná platba', amount === '—' ? '' : amount)
        + fact('Pôvodný dátum obnovy', renewal);
    } else if (status === 'unpaid') {
      title = 'Premium je pozastavené';
      description = 'Platbu sa nepodarilo obnoviť. V správe predplatného môžeš upraviť platobnú kartu.';
      facts = fact('Prístup skončil', cutoff);
    } else if (status === 'expired' || status === 'paused') {
      title = 'Predplatné skončilo';
      description = 'Premium už nie je aktívne. V správe zostávajú faktúry a možnosti účtu.';
      facts = fact('Prístup skončil', cutoff);
    } else if (!status && data.ma_narok === true) {
      title = 'Premium je aktívne';
      description = 'Tento prístup nemá predplatné na správu.';
    } else {
      title = 'Nemáš aktívne predplatné';
      description = 'Na tomto účte teraz neevidujeme spravované ročné Premium.';
    }

    const factList = facts ? `<dl class="subscription-facts">${facts}</dl>` : '';
    const action = canManage ? `<p class="muted" id="subscription-manage-copy">Zrušenie, obnovenie, platobnú kartu a faktúry spravuje Lemon Squeezy. Uvar.si stav iba zobrazuje.</p><button class="btn btn--ghost" id="subscription-manage" type="button" aria-describedby="subscription-manage-copy subscription-manage-status">Spravovať predplatné</button><p class="muted subscription-help" id="subscription-manage-status" role="status" aria-live="polite"></p>` : '';
    return `<section class="card subscription-card" aria-labelledby="subscription-title"><span class="eyebrow">Premium · predplatné</span><h2 id="subscription-title">${title}</h2><p class="muted">${description}</p>${factList}${action}</section>`;
  }

  function safeCustomerPortalDestination(value) {
    if (typeof value !== 'string' || !value || value !== value.trim() || value.length > 4096 || /[\u0000-\u001f\u007f]/.test(value) || /\s/.test(value)) return false;
    try {
      const parsed = new URL(value);
      const host = parsed.hostname.toLowerCase();
      return parsed.protocol === 'https:'
        && (host === 'lemonsqueezy.com' || host.endsWith('.lemonsqueezy.com'))
        && !parsed.username && !parsed.password && !parsed.port && !parsed.hash
        && parsed.pathname.startsWith('/') && parsed.pathname !== '/';
    } catch (_error) {
      return false;
    }
  }

  async function openSubscriptionPortal(button, status) {
    return runGuardedAction(button, status, async () => {
      button.setAttribute('aria-busy', 'true');
      status.textContent = 'Otváram bezpečnú správu predplatného…';
      try {
        const portal = await api('/api/platba/portal', {method:'POST'});
        if (!portal || !safeCustomerPortalDestination(portal.url))
          throw new Error('Neplatná adresa správy predplatného.');
        location.assign(portal.url);
      } catch (error) {
        if (error && error.authRequired) throw error;
        throw new Error(subscriptionPortalHelp());
      } finally {
        button.removeAttribute('aria-busy');
      }
    });
  }

  function bindSubscriptionManagement() {
    const button = $('#subscription-manage');
    const status = $('#subscription-manage-status');
    if (button && status) button.onclick = () => openSubscriptionPortal(button, status);
  }

  async function loadSubscriptionManagement() {
    const root = $('#subscription-management');
    if (!root) return false;
    try {
      const data = await api('/api/platba/stav');
      if ($('#subscription-management') !== root) return false;
      root.innerHTML = subscriptionManagementHtml(data);
      bindSubscriptionManagement();
      return true;
    } catch (error) {
      if ($('#subscription-management') !== root || (error && error.authRequired)) return false;
      root.innerHTML = subscriptionManagementHtml(null, {error:true});
      return false;
    }
  }

  function serviceStatus(status) {
    return ({received:'Prijaté',processing:'Spracúva sa',refunded:'Vrátené',
      requires_review:'Vyžaduje kontrolu',resolved:'Vybavené'})[status] || 'Prijaté';
  }

  function customerServiceHtml(data) {
    const orders = Array.isArray(data && data.orders) ? data.orders : [];
    const requests = Array.isArray(data && data.requests) ? data.requests : [];
    const order = orders[0];
    const rows = requests.length ? `<div class="request-list">${requests.map(item =>
      `<div class="request-row"><span>${item.request_type === 'withdrawal' ? 'Odstúpenie' : 'Reklamácia'}<br><small>${serviceDate(item.created_at)}</small></span><b class="request-status--${esc(item.status)}">${esc(serviceStatus(item.status))}</b></div>`
    ).join('')}</div>` : '<p class="muted" style="margin-top:12px">Zatiaľ tu nemáš žiadnu žiadosť.</p>';
    if (!order) return `<div class="card service-card"><h2>Platba a pomoc</h2><p class="muted">K tomuto účtu neevidujeme zaplatenú objednávku. Ak potrebuješ pomoc, napíš na <a href="mailto:pumaragency@gmail.com">pumaragency@gmail.com</a>.</p>${rows}</div>`;
    const deadline = serviceDate(order.refund_deadline);
    const inTime = Date.now() / 1000 <= Number(order.refund_deadline || 0);
    return `<div class="card service-card"><h2>Platba a pomoc</h2>
      <p class="muted">Žiadosť vybaví človek. Samotné odoslanie ešte neznamená, že refundácia prebehla.</p>
      <div class="service-order"><b>Zakladajúce Premium · 39 €</b><br>Dátum objednávky: ${serviceDate(order.purchased_at)} · 14-dňová lehota do ${deadline}<br>Číslo objednávky: ${esc(order.order_id)} · E-mail účtu: ${esc(ME.email)}</div>
      ${rows}
      <form id="withdrawal-form" class="service-form" data-order="${esc(order.order_id)}" data-purchased="${esc(order.purchased_at)}">
        <h3>Odstúpenie od zmluvy</h3>
        <p class="muted">Použitie tohto formulára nie je povinné. Odstúpiť môžeš aj akýmkoľvek jednoznačným oznámením na pumaragency@gmail.com.</p>
        <p class="muted">Dátum objednávky: ${serviceDate(order.purchased_at)} · Číslo objednávky: ${esc(order.order_id)} · E-mail účtu: ${esc(ME.email)}</p>
        <p class="muted">${inTime ? 'V 14-dňovej lehote ti vrátime celú platbu.' : 'Lehota už uplynula; žiadosť prijmeme na manuálne posúdenie.'} Refundácia ešte neprebehla.</p>
        <label>Meno a priezvisko<input id="withdrawal-name" maxlength="160" autocomplete="name" required></label>
        <label>Adresa spotrebiteľa<textarea id="withdrawal-address" maxlength="500" autocomplete="street-address" required></textarea></label>
        <label class="consent-row"><input id="withdrawal-confirm" type="checkbox" required><span>Žiadam o odstúpenie a rozumiem, že po úplnom vrátení platby stratím Premium.</span></label>
        <button class="btn btn--ghost" type="submit">Odoslať žiadosť o odstúpenie</button><p class="muted" id="withdrawal-status" aria-live="polite"></p>
      </form>
      <form id="complaint-form" class="service-form" data-order="${esc(order.order_id)}">
        <h3>Reklamácia</h3><label>Čo nie je v poriadku?<textarea id="complaint-message" maxlength="4000" required placeholder="Stručne opíš problém. Ak bude treba snímku, doplníš ju odpoveďou na e-mail."></textarea></label>
        <button class="btn btn--ghost" type="submit">Odoslať reklamáciu</button><p class="muted" id="complaint-status" aria-live="polite"></p>
      </form></div>`;
  }

  async function loadCustomerService() {
    const root = $('#customer-service');
    if (!root) return false;
    try {
      const data = await api('/api/consumer/requests');
      if ($('#customer-service') !== root) return false;
      root.outerHTML = `<div id="customer-service">${customerServiceHtml(data)}</div>`;
    } catch (error) {
      if ($('#customer-service') !== root || (error && error.authRequired)) return false;
      root.innerHTML = '<div class="card service-card"><h2>Platba a pomoc</h2><p class="muted">Prehľad sa nepodarilo načítať. Napíš na <a href="mailto:pumaragency@gmail.com">pumaragency@gmail.com</a>.</p></div>';
      return false;
    }
    const withdrawal = $('#withdrawal-form');
    if (withdrawal) withdrawal.onsubmit = event => {
      event.preventDefault();
      const button = withdrawal.querySelector('button'), status = $('#withdrawal-status');
      if (!$('#withdrawal-confirm').checked) {
        status.textContent = 'Najprv potvrď, že rozumieš následku úplnej refundácie.';
        return;
      }
      runGuardedAction(button, status, async () => {
        const name = $('#withdrawal-name').value.trim();
        const address = $('#withdrawal-address').value.trim();
        const sentAt = new Date().toISOString().slice(0, 10);
        const message = [
          'Oznamujem, že odstupujem od zmluvy o digitálnej službe Uvar.si.',
          'Meno a priezvisko spotrebiteľa: ' + name,
          'Adresa spotrebiteľa: ' + address,
          'E-mail účtu: ' + ME.email,
          'Číslo objednávky: ' + withdrawal.dataset.order,
          'Dátum objednávky: ' + withdrawal.dataset.purchased,
          'Dátum odoslania: ' + sentAt
        ].join('\n');
        await api('/api/consumer/withdrawal', {method:'POST', body:JSON.stringify({order_id:withdrawal.dataset.order,message:message})});
        await loadCustomerService();
      });
    };
    const complaint = $('#complaint-form');
    if (complaint) complaint.onsubmit = event => {
      event.preventDefault();
      const button = complaint.querySelector('button'), status = $('#complaint-status');
      runGuardedAction(button, status, async () => {
        await api('/api/consumer/complaint', {method:'POST', body:JSON.stringify({order_id:complaint.dataset.order,message:$('#complaint-message').value})});
        await loadCustomerService();
      });
    };
    return true;
  }

  global.UvarsiSubscription = Object.freeze({
    loadSubscription: loadSubscriptionManagement,
    loadCustomerService: loadCustomerService,
    date: serviceDate,
    render: subscriptionManagementHtml,
    safeDestination: safeCustomerPortalDestination,
    open: openSubscriptionPortal
  });
})(window);
