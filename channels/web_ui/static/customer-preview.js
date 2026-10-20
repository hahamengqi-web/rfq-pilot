(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const copy = {
    zh: {
      'nav.conversations': '对话', 'portal.label': '智能客服', 'portal.title': '聊天记录',
      'chat.new': '新建对话', 'chat.search': '搜索对话', 'chat.recent': '最近对话',
      'chat.inquiry': '产品咨询', 'status.online': '在线服务', 'status.connecting': '正在连接', 'status.loginRequired': '登录后开始咨询',
      'status.consultation': '产品与售后咨询', 'status.quote': '报价申请', 'chat.delete': '删除对话',
      'assistant.identity': '产品客服',
      'welcome.title': '有什么问题，可以直接问我',
      'welcome.subtitle': '告诉我产品型号和你的问题，我会根据产品资料回答参数、使用方法和售后问题。',
      'chat.inputLabel': '咨询内容', 'chat.placeholder': '输入产品型号和你的问题…',
      'chat.send': '发送消息', 'chat.quote': '售后咨询',
      'chat.quoteTemplate': '我想申请报价：\n产品：\n数量：\n目的地：\n贸易术语：\n期望交期：',
      'chat.serviceUnavailable': '服务暂时不可用，请稍后重试或联系销售团队。',
      'chat.pending': '正在查询产品资料，请稍候…',
      'chat.progress.received': '已收到问题，正在理解你的需求…',
      'chat.progress.routing': '正在识别产品和咨询类型…',
      'chat.progress.searching': '正在检索匹配的产品资料…',
      'chat.progress.generating': '已找到资料，正在整理答案…',
      'citation.title': '引用来源', 'citation.page': '第 {page} 页',
      'feedback.helpful': '有帮助', 'feedback.notHelpful': '没帮助', 'feedback.saved': '感谢反馈',
      'support.human': '人工客服', 'support.completed': '人工客服已完成本次处理。',
      'portal.privacy': '产品回答来自知识库；重要参数和售后操作请核对所引资料。',
      'prompt.usbcLabel': '产品参数', 'prompt.usbcText': 'Archer AX55 有几个 LAN 口？',
      'prompt.tabletLabel': '使用与售后', 'prompt.tabletText': '我需要产品售后帮助。产品型号：Archer AX55；问题现象：',
      'chat.newTitle': '新对话', 'chat.newHint': '输入产品型号和问题，开始咨询。',
      'common.cancel': '取消', 'common.create': '开始咨询', 'common.close': '关闭', 'time.today': '今天', 'time.yesterday': '昨天',
      'auth.account': '客户账号', 'auth.loginButton': '客户登录', 'auth.title': '客户登录', 'auth.hint': '登录后可恢复本账号历史对话。',
      'auth.email': '邮箱', 'auth.password': '密码', 'auth.login': '登录', 'auth.logout': '退出', 'auth.register': '注册',
      'auth.emailPlaceholder': 'name@example.com', 'auth.passwordPlaceholder': '请输入密码', 'auth.security': '登录信息将通过安全连接提交。',
      'memory.manage': '管理记忆', 'memory.label': 'LONG-TERM MEMORY', 'memory.title': '我的长期记忆',
      'memory.hint': '这里只显示你明确要求保存的信息。', 'memory.empty': '目前没有长期记忆。',
      'memory.delete': '删除', 'memory.deleteAll': '删除全部记忆', 'memory.deleted': '记忆已删除。',
      'memory.deleteConfirm': '确定删除这条记忆吗？', 'memory.deleteAllConfirm': '确定删除全部长期记忆吗？'
    },
    en: {
      'nav.conversations': 'Conversations', 'portal.label': 'CUSTOMER PORTAL', 'portal.title': 'My conversations',
      'chat.new': 'Start a chat', 'chat.search': 'Search conversations', 'chat.recent': 'Recent conversations',
      'chat.inquiry': 'PRODUCT CONSULTATION', 'status.online': 'Online', 'status.connecting': 'Connecting', 'status.loginRequired': 'Sign in to start',
      'status.consultation': 'Product consultation', 'status.quote': 'Quote request', 'chat.delete': 'Delete conversation',
      'assistant.identity': 'Product Support',
      'welcome.title': 'How can I help with your product?',
      'welcome.subtitle': 'Ask about specifications, setup, or after-sales support for products in our knowledge base.',
      'chat.inputLabel': 'Message', 'chat.placeholder': 'Enter a product model and your question…',
      'chat.send': 'Send message', 'chat.quote': 'After-sales',
      'chat.quoteTemplate': 'I would like a quotation:\nProduct:\nQuantity:\nDestination:\nIncoterm:\nRequired delivery date:',
      'chat.serviceUnavailable': 'The service is temporarily unavailable. Please try again later or contact our sales team.',
      'chat.pending': 'Checking product information…',
      'chat.progress.received': 'Question received. Understanding your need…',
      'chat.progress.routing': 'Identifying the product and request type…',
      'chat.progress.searching': 'Searching matching product material…',
      'chat.progress.generating': 'Sources found. Preparing the answer…',
      'citation.title': 'Sources', 'citation.page': 'Page {page}',
      'feedback.helpful': 'Helpful', 'feedback.notHelpful': 'Not helpful', 'feedback.saved': 'Thanks for your feedback',
      'support.human': 'Human support', 'support.completed': 'Human support has completed this request.',
      'portal.privacy': 'Answers come from our knowledge base. Check the cited materials for important specifications and support steps.',
      'prompt.usbcLabel': 'Specifications', 'prompt.usbcText': 'How many LAN ports does the Archer AX55 have?',
      'prompt.tabletLabel': 'Setup and support', 'prompt.tabletText': 'I need product support. Model: Archer AX55. Issue: ',
      'chat.newTitle': 'New chat', 'chat.newHint': 'Enter a product model and your question to start.',
      'common.cancel': 'Cancel', 'common.create': 'Start chat', 'common.close': 'Close', 'time.today': 'Today', 'time.yesterday': 'Yesterday',
      'auth.account': 'Customer account', 'auth.loginButton': 'Sign in', 'auth.title': 'Customer login', 'auth.hint': 'Sign in to restore your account conversations.',
      'auth.email': 'Email', 'auth.password': 'Password', 'auth.login': 'Sign in', 'auth.logout': 'Sign out', 'auth.register': 'Register',
      'auth.emailPlaceholder': 'name@example.com', 'auth.passwordPlaceholder': 'Enter your password', 'auth.security': 'Your sign-in details are submitted over a secure connection.',
      'memory.manage': 'Manage memory', 'memory.label': 'LONG-TERM MEMORY', 'memory.title': 'My saved memories',
      'memory.hint': 'Only information you explicitly asked to save appears here.', 'memory.empty': 'No long-term memories yet.',
      'memory.delete': 'Delete', 'memory.deleteAll': 'Delete all memories', 'memory.deleted': 'Memory deleted.',
      'memory.deleteConfirm': 'Delete this memory?', 'memory.deleteAllConfirm': 'Delete all long-term memories?'
    },
    de: {
      'nav.conversations': 'Gespräche', 'portal.label': 'KUNDENPORTAL', 'portal.title': 'Meine Gespräche',
      'chat.new': 'Beratung starten', 'chat.search': 'Gespräche suchen', 'chat.recent': 'Letzte Gespräche',
      'chat.inquiry': 'PRODUKTBERATUNG', 'status.online': 'Online', 'status.connecting': 'Verbindung', 'status.loginRequired': 'Zum Start anmelden',
      'status.consultation': 'Produktberatung', 'status.quote': 'Angebotsanfrage', 'chat.delete': 'Gespräch löschen',
      'assistant.identity': 'Produktservice',
      'welcome.title': 'Wie können wir beim Produkt helfen?',
      'welcome.subtitle': 'Fragen Sie zu technischen Daten, Einrichtung oder Kundendienst für Produkte in unserer Wissensdatenbank.',
      'chat.inputLabel': 'Nachricht', 'chat.placeholder': 'Produktmodell und Frage eingeben…',
      'chat.send': 'Nachricht senden', 'chat.quote': 'Kundendienst',
      'chat.quoteTemplate': 'Ich möchte ein Angebot anfordern:\nProdukt:\nMenge:\nZielort:\nIncoterm:\nGewünschter Liefertermin:',
      'chat.serviceUnavailable': 'Der Dienst ist vorübergehend nicht verfügbar. Bitte versuchen Sie es später erneut oder kontaktieren Sie den Vertrieb.',
      'chat.pending': 'Produktinformationen werden geprüft…',
      'chat.progress.received': 'Frage erhalten. Bedarf wird verstanden…',
      'chat.progress.routing': 'Produkt und Anfrageart werden erkannt…',
      'chat.progress.searching': 'Passende Produktunterlagen werden durchsucht…',
      'chat.progress.generating': 'Quellen gefunden. Antwort wird vorbereitet…',
      'citation.title': 'Quellen', 'citation.page': 'Seite {page}',
      'feedback.helpful': 'Hilfreich', 'feedback.notHelpful': 'Nicht hilfreich', 'feedback.saved': 'Danke für Ihr Feedback',
      'support.human': 'Kundendienst', 'support.completed': 'Der Kundendienst hat diese Anfrage abgeschlossen.',
      'portal.privacy': 'Antworten stammen aus der Wissensdatenbank. Prüfen Sie wichtige technische Daten und Schritte anhand der Quellen.',
      'prompt.usbcLabel': 'Technische Daten', 'prompt.usbcText': 'Wie viele LAN-Anschlüsse hat der Archer AX55?',
      'prompt.tabletLabel': 'Einrichtung und Hilfe', 'prompt.tabletText': 'Ich benötige Hilfe mit einem Produkt. Modell: Archer AX55. Problem: ',
      'chat.newTitle': 'Neuer Chat', 'chat.newHint': 'Geben Sie ein Produktmodell und Ihre Frage ein.',
      'common.cancel': 'Abbrechen', 'common.create': 'Beratung starten', 'common.close': 'Schließen', 'time.today': 'Heute', 'time.yesterday': 'Gestern',
      'auth.account': 'Kundenkonto', 'auth.loginButton': 'Anmelden', 'auth.title': 'Kundenanmeldung', 'auth.hint': 'Melden Sie sich an, um Ihre Anfragen wiederherzustellen.',
      'auth.email': 'E-Mail', 'auth.password': 'Passwort', 'auth.login': 'Anmelden', 'auth.logout': 'Abmelden', 'auth.register': 'Registrieren',
      'auth.emailPlaceholder': 'name@example.com', 'auth.passwordPlaceholder': 'Passwort eingeben', 'auth.security': 'Ihre Anmeldedaten werden über eine sichere Verbindung übertragen.',
      'memory.manage': 'Erinnerungen', 'memory.label': 'LONG-TERM MEMORY', 'memory.title': 'Gespeicherte Erinnerungen',
      'memory.hint': 'Hier erscheinen nur ausdrücklich gespeicherte Informationen.', 'memory.empty': 'Noch keine langfristigen Erinnerungen.',
      'memory.delete': 'Löschen', 'memory.deleteAll': 'Alle Erinnerungen löschen', 'memory.deleted': 'Erinnerung gelöscht.',
      'memory.deleteConfirm': 'Diese Erinnerung löschen?', 'memory.deleteAllConfirm': 'Alle langfristigen Erinnerungen löschen?'
    }
  };

  const browserLanguage = (navigator.language || 'en').toLowerCase();
  let lang = localStorage.getItem('nanoclaw-customer-language')
    || (browserLanguage.startsWith('zh') ? 'zh' : browserLanguage.startsWith('de') ? 'de' : 'en');
  const t = (key, values = {}) => Object.entries(values).reduce(
    (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
    copy[lang][key] || copy.en[key] || key,
  );
  let conversations = [];
  let active = '';
  const messagesByConversation = {};
  const pendingRequests = new Map();
  const requestProgress = new Map();
  const welcomeMarkup = $('messages').innerHTML;
  let socket = null;
  let reconnectTimer = null;
  let authenticated = false;
  let authRequired = false;
  let csrfToken = '';
  let creatingConversation = false;
  const anonymousStorageKey = 'nanoclaw-customer-chat-history-v1';
  const saveAnonymous = () => {
    if (authenticated) return;
    try {
      const recent = conversations.slice(0, 50);
      localStorage.setItem(anonymousStorageKey, JSON.stringify({
        conversations: recent,
        messages: Object.fromEntries(recent.map(item => [item.id, (messagesByConversation[item.id] || []).slice(-200)])),
        active,
      }));
    } catch (_error) { /* Private browsing may block local storage. */ }
  };
  const restoreAnonymous = () => {
    try {
      const saved = JSON.parse(localStorage.getItem(anonymousStorageKey) || '{}');
      if (!Array.isArray(saved.conversations)) return;
      conversations = saved.conversations.filter(item => typeof item.id === 'string' && typeof item.title === 'string').slice(0, 50);
      for (const item of conversations) messagesByConversation[item.id] = Array.isArray(saved.messages?.[item.id]) ? saved.messages[item.id].slice(-200) : [];
      active = conversations.some(item => item.id === saved.active) ? saved.active : conversations[0]?.id || '';
      $('conversationTitle').textContent = conversations.find(item => item.id === active)?.title || t('chat.newTitle');
      renderList();
      renderMessages();
    } catch (_error) { /* Ignore old or malformed browser data. */ }
  };

  const updateAccountButton = () => {
    const label = t(authenticated ? 'auth.account' : 'auth.loginButton');
    $('customerAccountLabel').textContent = label;
    $('customerAccountLabel').dataset.i18n = authenticated ? 'auth.account' : 'auth.loginButton';
    $('customerAccount').setAttribute('aria-label', label);
  };

  const cookieValue = name => document.cookie.split('; ').find(item => item.startsWith(`${name}=`))?.split('=').slice(1).join('=') || '';
  const customerApi = async (path, options = {}) => {
    const headers = { ...(options.headers || {}) };
    if (csrfToken && !['GET', 'HEAD'].includes((options.method || 'GET').toUpperCase())) {
      headers['X-CSRF-Token'] = decodeURIComponent(csrfToken);
    }
    const response = await fetch(path, { credentials: 'same-origin', ...options, headers });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`);
    return response.status === 204 ? null : response.json();
  };

  const loadServerMessages = async conversationId => {
    if (!authenticated) return;
    const payload = await customerApi(`/api/customer/conversations/${encodeURIComponent(conversationId)}/messages?limit=200`);
    messagesByConversation[conversationId] = payload.items
      .filter(item => item.role === 'user' || item.role === 'assistant')
      .map(item => {
        const structured = item.content && typeof item.content === 'object' ? item.content : null;
        return {
          role: item.role,
          content: typeof item.content === 'string' ? item.content : (structured?.text || JSON.stringify(item.content)),
          citations: Array.isArray(structured?.citations) ? structured.citations : [],
          source: structured?.source || '', requestId: item.request_id || '',
        };
      });
  };

  const loadServerConversations = async () => {
    const sessionResponse = await fetch('/api/customer/auth/session', { credentials: 'same-origin' });
    if (!sessionResponse.ok) {
      authRequired = sessionResponse.status !== 404;
      restoreAnonymous();
      return false;
    }
    authRequired = true;
    const session = await sessionResponse.json();
    csrfToken = cookieValue('nanoclaw_customer_csrf');
    if (!session.authenticated) {
      authenticated = false;
      updateAccountButton();
      restoreAnonymous();
      return false;
    }
    const payload = await customerApi('/api/customer/conversations?limit=100');
    authenticated = true;
    updateAccountButton();
    conversations = payload.items.map(item => ({
      id: item.conversation_id, title: item.title, updatedAt: item.updated_at, version: item.version,
      kind: 'consultation',
    }));
    conversations.forEach(item => { messagesByConversation[item.id] ||= []; });
    active = conversations[0]?.id || '';
    if (active) await loadServerMessages(active);
    $('conversationTitle').textContent = conversations[0]?.title || t('chat.newTitle');
    renderList();
    renderMessages();
    return true;
  };

  const technicalErrorPattern = /(API\s*调用失败|Error\s*code\s*:\s*\d+|Token\s+is\s+invalid|invalid\s+(?:api\s+)?key|authentication\s+failed)/i;
  const safeAssistantMessage = value => technicalErrorPattern.test(value) || /<\/?(?:tool_call|toolcall|arg_key|arg_value)\b/i.test(value)
    ? t('chat.serviceUnavailable') : value;

  const appendMessage = (role, value, citations = [], source = '', requestId = '') => {
    if (role === 'status') {
      const row = document.createElement('article');
      row.className = 'portal-message-row assistant';
      const message = document.createElement('div');
      message.className = 'preview-message assistant';
      message.setAttribute('role', 'status');
      message.textContent = value;
      row.append(message);
      $('messages').append(row);
      return;
    }
    const row = document.createElement('article');
    row.className = `portal-message-row ${role}`;
    const avatar = document.createElement('span');
    const isAssistant = role === 'assistant';
    avatar.className = `portal-message-avatar ${isAssistant ? 'brand-avatar' : 'customer-avatar'}`;
    avatar.setAttribute('role', 'img');
    avatar.setAttribute('aria-label', isAssistant ? t('assistant.identity') : (lang === 'zh' ? '客户' : lang === 'de' ? 'Kunde' : 'Customer'));
    avatar.innerHTML = isAssistant
      ? '<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M10.5 11.5 7 7m14.5 4.5L25 7M10 12c-3.7.7-5.2 4.8-3 7.8 1.8 2.5 5.5 2.8 8 .7m7-8.5c3.7.7 5.2 4.8 3 7.8-1.8 2.5-5.5 2.8-8 .7"/><path d="M11 12.5c0-2.4 2.2-4.5 5-4.5s5 2.1 5 4.5v7c0 2.7-2.2 4.8-5 4.8s-5-2.1-5-4.8z"/><circle cx="14" cy="14" r="1"/><circle cx="18" cy="14" r="1"/></svg>'
      : '<svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="11.5" r="5"/><path d="M7.5 26c.8-5.5 4-8.3 8.5-8.3s7.7 2.8 8.5 8.3"/></svg>';
    const message = document.createElement('div');
    message.className = `preview-message ${role}`;
    if (isAssistant) {
      message.classList.add('markdown-body');
      if (source === 'human') message.classList.add('human-support-message');
      message.innerHTML = window.NanoClawMarkdown?.render(safeAssistantMessage(value)) || '';
      if (Array.isArray(citations) && citations.length) {
        const details = document.createElement('details');
        details.className = 'citation-details';
        const summary = document.createElement('summary');
        summary.textContent = `${t('citation.title')} · ${citations.length}`;
        details.append(summary);
        citations.forEach((citation, index) => {
          const card = document.createElement('article');
          card.className = 'citation-card';
          const title = document.createElement('strong');
          title.textContent = `【${index + 1}】 ${citation.title || '产品资料'}`;
          const meta = document.createElement('small');
          const location = citation.page_number
            ? t('citation.page', { page: citation.page_number })
            : (citation.location || '');
          meta.textContent = location;
          const excerpt = document.createElement('p');
          excerpt.textContent = citation.excerpt || '';
          card.append(title, meta, excerpt);
          details.append(card);
        });
        message.append(details);
      }
      if (authenticated && requestId && source !== 'human') {
        const feedback = document.createElement('div');
        feedback.className = 'answer-feedback';
        const addButton = (helpful, label) => {
          const button = document.createElement('button');
          button.type = 'button';
          button.textContent = label;
          button.onclick = async () => {
            feedback.querySelectorAll('button').forEach(item => { item.disabled = true; });
            try {
              await customerApi(`/api/customer/conversations/${encodeURIComponent(active)}/feedback`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ request_id: requestId, helpful }),
              });
              feedback.querySelector('small').textContent = t('feedback.saved');
            } catch (_error) {
              feedback.querySelectorAll('button').forEach(item => { item.disabled = false; });
            }
          };
          feedback.append(button);
        };
        addButton(true, t('feedback.helpful'));
        addButton(false, t('feedback.notHelpful'));
        const status = document.createElement('small');
        feedback.append(status);
        message.append(feedback);
      }
    } else {
      message.textContent = value;
    }
    if (isAssistant) row.append(avatar, message);
    else row.append(message, avatar);
    $('messages').append(row);
  };

  const renderMessages = () => {
    const messages = messagesByConversation[active] || [];
    $('deleteConversation').disabled = !active;
    $('messages').innerHTML = messages.length ? '' : welcomeMarkup;
    messages.forEach(item => appendMessage(
      item.role, item.content, item.citations || [], item.source || '', item.requestId || '',
    ));
    for (const [requestId, conversationId] of pendingRequests.entries()) {
      if (conversationId !== active) continue;
      const stage = requestProgress.get(requestId);
      appendMessage('status', stage ? t(`chat.progress.${stage}`) : t('chat.pending'));
    }
    $('messages').scrollTop = $('messages').scrollHeight;
  };

  const updateConversationMode = () => {
    $('inquiryNumber').textContent = t('status.consultation');
  };

  const setConnection = online => {
    const status = document.querySelector('.status-pill span');
    if (status) status.textContent = t(online ? 'status.online' : 'status.connecting');
    $('inquiryForm').querySelector('button[type="submit"]').disabled = !online;
  };

  const setLoginRequired = () => {
    const status = document.querySelector('.status-pill span');
    if (status) status.textContent = t('status.loginRequired');
    $('inquiryForm').querySelector('button[type="submit"]').disabled = true;
  };

  const connect = () => {
    if (authRequired && !authenticated) {
      setLoginRequired();
      return;
    }
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${location.host}/ws`);
    setConnection(false);
    socket.onopen = () => { setConnection(true); pollSupportUpdates(); };
    socket.onmessage = event => {
      let payload;
      try { payload = JSON.parse(event.data); } catch (_error) { return; }
      if (payload.type === 'error') {
        const conversationId = pendingRequests.get(payload.request_id);
        if (!conversationId) return;
        pendingRequests.delete(payload.request_id);
        requestProgress.delete(payload.request_id);
        messagesByConversation[conversationId]?.push({ role: 'assistant', content: t('chat.serviceUnavailable') });
        saveAnonymous();
        if (conversationId === active) renderMessages();
        return;
      }
      if (payload.type === 'assistant.progress') {
        const conversationId = pendingRequests.get(payload.request_id) || payload.conversation_id;
        if (!conversationId) return;
        requestProgress.set(payload.request_id, payload.metadata?.stage || 'received');
        if (conversationId === active) renderMessages();
        return;
      }
      if (payload.type !== 'assistant.message' || typeof payload.content !== 'string') return;
      const conversationId = payload.conversation_id;
      if (!messagesByConversation[conversationId]) return;
      pendingRequests.delete(payload.request_id);
      requestProgress.delete(payload.request_id);
      messagesByConversation[conversationId].push({
        role: 'assistant', content: payload.content,
        citations: Array.isArray(payload.citations) ? payload.citations : [],
        requestId: payload.request_id || '',
      });
      saveAnonymous();
      if (conversationId === active) renderMessages();
    };
    socket.onclose = () => {
      socket = null;
      setConnection(false);
      for (const conversationId of pendingRequests.values()) {
        messagesByConversation[conversationId]?.push({ role: 'assistant', content: t('chat.serviceUnavailable') });
      }
      pendingRequests.clear();
      requestProgress.clear();
      saveAnonymous();
      renderMessages();
      clearTimeout(reconnectTimer);
      if (authRequired && !authenticated) {
        setLoginRequired();
        return;
      }
      reconnectTimer = setTimeout(connect, 3000);
    };
    socket.onerror = () => socket.close();
  };

  const pollSupportUpdates = async () => {
    if (!active) return;
    try {
      const payload = await customerApi(
        `/api/public/support-updates?conversation_id=${encodeURIComponent(active)}&limit=100`,
      );
      const messages = messagesByConversation[active] || (messagesByConversation[active] = []);
      const seen = new Set(messages.map(item => item.supportUpdateId).filter(Boolean));
      let changed = false;
      for (const update of payload.items || []) {
        if (!update.update_id || seen.has(update.update_id)) continue;
        const content = update.type === 'completed'
          ? t('support.completed')
          : `${t('support.human')}：${update.message}`;
        messages.push({
          role: 'assistant', content, source: 'human',
          supportUpdateId: update.update_id,
        });
        seen.add(update.update_id);
        changed = true;
      }
      if (changed) {
        saveAnonymous();
        renderMessages();
      }
    } catch (_error) { /* The next poll retries automatically. */ }
  };
  window.setInterval(pollSupportUpdates, 3000);

  const renderList = () => {
    const list = $('conversationList');
    list.replaceChildren();
    const query = $('conversationSearch').value.trim().toLocaleLowerCase();
    for (const conversation of conversations.filter(item => item.title.toLocaleLowerCase().includes(query))) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `customer-conversation ${conversation.id === active ? 'active' : ''}`;
      button.dataset.id = conversation.id;
      const title = document.createElement('strong');
      title.textContent = conversation.title;
      const meta = document.createElement('small');
      const date = conversation.updatedAt ? new Date(conversation.updatedAt) : null;
      const dateLabel = date && !Number.isNaN(date.getTime()) ? date.toLocaleDateString(lang === 'zh' ? 'zh-CN' : lang === 'de' ? 'de-DE' : 'en-US') : t('time.today');
      meta.textContent = `${dateLabel} · ${t('status.consultation')}`;
      button.append(title, meta);
      list.append(button);
    }
    if (!conversations.length) {
      const empty = document.createElement('p');
      empty.className = 'conversation-empty';
      empty.textContent = lang === 'zh' ? '还没有聊天记录。输入问题即可开始。' : lang === 'de' ? 'Noch keine Gespräche. Stellen Sie eine Frage.' : 'No conversations yet. Ask a question to begin.';
      list.append(empty);
    }
    $('chatCount').textContent = conversations.length;
    list.querySelectorAll('button').forEach(button => {
      button.onclick = async () => {
        active = button.dataset.id;
        $('conversationTitle').textContent = conversations.find(item => item.id === active).title;
        if (authenticated) await loadServerMessages(active);
        saveAnonymous();
        renderList();
        renderMessages();
        updateConversationMode();
      };
    });
  };
  $('conversationSearch').addEventListener('input', renderList);

  const setLanguage = next => {
    lang = copy[next] ? next : 'en';
    localStorage.setItem('nanoclaw-customer-language', lang);
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : lang;
    $('languageToggle').textContent = { zh: '中', en: 'EN', de: 'DE' }[lang];
    document.querySelectorAll('[data-i18n]').forEach(element => {
      element.textContent = t(element.dataset.i18n);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(element => {
      element.placeholder = t(element.dataset.i18nPlaceholder);
    });
    document.querySelectorAll('[data-i18n-aria]').forEach(element => {
      element.setAttribute('aria-label', t(element.dataset.i18nAria));
    });
    updateAccountButton();
    renderList();
    updateConversationMode();
  };

  const dialog = $('customerDialog');
  const authDialog = $('customerAuthDialog');
  const memoryDialog = $('customerMemoryDialog');
  $('customerAccount').onclick = () => {
    $('customerAuthStatus').textContent = t('auth.hint');
    $('customerAuthStatus').dataset.state = 'neutral';
    $('customerLogout').hidden = !authenticated;
    $('customerMemories').hidden = !authenticated;
    $('customerLogin').hidden = authenticated;
    $('customerRegister').hidden = authenticated;
    document.querySelector('.auth-fields').hidden = authenticated;
    authDialog.showModal();
  };
  $('customerAuthForm').onsubmit = async event => {
    event.preventDefault();
    const submitter = event.submitter;
    if (submitter?.value === 'cancel' || submitter?.hasAttribute('data-auth-close')) {
      authDialog.close('cancel');
      return;
    }
    try {
      const credentials = { email: $('customerAuthEmail').value,
        password: $('customerAuthPassword').value, locale: lang };
      if (submitter?.value === 'register') {
        await customerApi('/api/customer/auth/register', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(credentials),
        });
      }
      await customerApi('/api/customer/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(credentials),
      });
      csrfToken = cookieValue('nanoclaw_customer_csrf');
      authDialog.close();
      await loadServerConversations();
      if (socket) socket.close();
      else connect();
    } catch (_error) {
      $('customerAuthStatus').textContent = lang === 'zh' ? '登录或注册失败，请检查邮箱和密码。' : lang === 'de' ? 'Anmeldung oder Registrierung fehlgeschlagen.' : 'Sign-in or registration failed.';
      $('customerAuthStatus').dataset.state = 'error';
    } finally {
      $('customerAuthPassword').value = '';
    }
  };
  $('customerLogout').onclick = async () => {
    await customerApi('/api/customer/auth/logout', { method: 'POST' });
    authenticated = false;
    authDialog.close();
    location.reload();
  };
  const loadCustomerMemories = async () => {
    const root = $('customerMemoryList');
    root.replaceChildren();
    $('customerMemoryStatus').textContent = '';
    try {
      const payload = await customerApi('/api/customer/memories');
      const items = (payload.items || []).filter(item => item.status === 'active');
      if (!items.length) {
        const empty = document.createElement('p');
        empty.className = 'memory-empty';
        empty.textContent = t('memory.empty');
        root.append(empty);
        $('customerMemoryDeleteAll').disabled = true;
        return;
      }
      $('customerMemoryDeleteAll').disabled = false;
      for (const item of items) {
        const card = document.createElement('article');
        const content = document.createElement('div');
        const title = document.createElement('strong');
        title.textContent = item.summary || item.content;
        const body = document.createElement('p');
        body.textContent = item.content;
        const meta = document.createElement('small');
        meta.textContent = new Date(item.updated_at || item.created_at).toLocaleString();
        content.append(title, body, meta);
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.textContent = t('memory.delete');
        remove.onclick = async () => {
          if (!window.confirm(t('memory.deleteConfirm'))) return;
          await customerApi(`/api/customer/memories/${encodeURIComponent(item.memory_id)}?version=${encodeURIComponent(item.version)}`, { method: 'DELETE' });
          $('customerMemoryStatus').textContent = t('memory.deleted');
          await loadCustomerMemories();
        };
        card.append(content, remove);
        root.append(card);
      }
    } catch (error) {
      $('customerMemoryStatus').textContent = error.message;
    }
  };
  $('customerMemories').onclick = async () => {
    authDialog.close();
    memoryDialog.showModal();
    await loadCustomerMemories();
  };
  $('customerMemoryClose').onclick = () => memoryDialog.close();
  $('customerMemoryDone').onclick = () => memoryDialog.close();
  $('customerMemoryDeleteAll').onclick = async () => {
    if (!window.confirm(t('memory.deleteAllConfirm'))) return;
    await customerApi('/api/customer/memories', {
      method: 'DELETE', headers: { 'Idempotency-Key': crypto.randomUUID() },
    });
    $('customerMemoryStatus').textContent = t('memory.deleted');
    await loadCustomerMemories();
  };
  $('newInquiry').onclick = () => {
    active = '';
    $('conversationTitle').textContent = t('chat.newTitle');
    saveAnonymous();
    renderList();
    renderMessages();
    updateConversationMode();
    $('inquiryInput').focus();
  };
  dialog.addEventListener('close', async () => {
    if (dialog.returnValue !== 'confirm') return;
    let created;
    if (authenticated) {
      created = await customerApi('/api/customer/conversations', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: t('chat.newTitle') }),
      });
    }
    const id = created?.conversation_id || crypto.randomUUID();
    conversations.unshift({ id, title: created?.title || t('chat.newTitle'),
      updatedAt: new Date().toISOString(), version: created?.version || 1, kind: 'consultation' });
    messagesByConversation[id] = [];
    active = id;
    saveAnonymous();
    $('conversationTitle').textContent = t('chat.newTitle');
    renderList();
    renderMessages();
    updateConversationMode();
    $('inquiryInput').focus();
  });
  $('deleteConversation').onclick = async () => {
    const current = conversations.find(conversation => conversation.id === active);
    if (authenticated && current) {
      await customerApi(`/api/customer/conversations/${encodeURIComponent(active)}?version=${current.version}`, {
        method: 'DELETE', headers: { 'Idempotency-Key': crypto.randomUUID() },
      });
    }
    conversations = conversations.filter(conversation => conversation.id !== active);
    delete messagesByConversation[active];
    active = conversations[0]?.id || '';
    saveAnonymous();
    $('conversationTitle').textContent = conversations[0]?.title || t('chat.newTitle');
    renderList();
    renderMessages();
    updateConversationMode();
  };
  $('inquiryForm').onsubmit = async event => {
    event.preventDefault();
    const value = $('inquiryInput').value.trim();
    if (!value) return;
    if (creatingConversation) return;
    if (socket?.readyState !== WebSocket.OPEN) return;
    if (!active) {
      creatingConversation = true;
      const title = value.replace(/\s+/g, ' ').slice(0, 48);
      let created;
      try {
        if (authenticated) created = await customerApi('/api/customer/conversations', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title }),
        });
      } catch (_error) {
        appendMessage('status', t('chat.serviceUnavailable'));
        creatingConversation = false;
        return;
      }
      creatingConversation = false;
      active = created?.conversation_id || crypto.randomUUID();
      conversations.unshift({ id: active, title: created?.title || title,
        updatedAt: new Date().toISOString(), version: created?.version || 1, kind: 'consultation' });
      messagesByConversation[active] = [];
      $('conversationTitle').textContent = title;
      saveAnonymous();
      renderList();
    }
    const requestId = crypto.randomUUID();
    pendingRequests.set(requestId, active);
    requestProgress.set(requestId, 'received');
    messagesByConversation[active].push({ role: 'user', content: value });
    const activeConversation = conversations.find(item => item.id === active);
    if (activeConversation) activeConversation.updatedAt = new Date().toISOString();
    saveAnonymous();
    renderMessages();
    renderList();
    updateConversationMode();
    socket.send(JSON.stringify({ type: 'chat.message', protocol_version: 2,
      conversation_id: active, request_id: requestId, language: lang, content: value }));
    $('inquiryInput').value = '';
  };
  $('inquiryInput').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $('inquiryForm').requestSubmit();
    }
  });
  $('quoteRequest').onclick = () => {
    $('inquiryInput').value = lang === 'zh' ? '我遇到一个产品售后问题：\n产品型号：\n问题描述：' : lang === 'de' ? 'Ich brauche Hilfe mit einem Produkt:\nModell:\nProblem:' : 'I need after-sales help with a product:\nModel:\nIssue:';
    $('inquiryInput').focus();
  };
  document.querySelectorAll('[data-prompt-key]').forEach(button => {
    button.onclick = () => {
      $('inquiryInput').value = t(button.dataset.promptKey);
      $('inquiryInput').focus();
    };
  });
  $('languageToggle').onclick = () => $('languageMenu').classList.toggle('open');
  document.querySelectorAll('[data-language]').forEach(button => {
    button.onclick = () => {
      $('languageMenu').classList.remove('open');
      $('languageToggle').setAttribute('aria-expanded', 'false');
      setLanguage(button.dataset.language);
    };
  });
  window.addEventListener('storage', event => {
    if (event.key === 'nanoclaw-customer-language' && copy[event.newValue]) {
      setLanguage(event.newValue);
    }
  });
  window.addEventListener('nanoclaw:customer-language-change', event => {
    if (copy[event.detail?.language]) setLanguage(event.detail.language);
  });
  setLanguage(lang);
  renderMessages();
  loadServerConversations().then(loggedIn => {
    if (loggedIn || !authRequired) connect();
    else {
      setLoginRequired();
      authDialog.showModal();
    }
  }).catch(() => {
    if (authRequired) setLoginRequired();
    else connect();
  });
})();
