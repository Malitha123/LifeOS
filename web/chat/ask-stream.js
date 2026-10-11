// Chat SSE client: streams an answer from /api/ask/stream and the
// orchestrator's engine-handoff to /api/chat/handoff. Extracted verbatim from
// index.html's inline <script>, with the transport split into askStream() so
// follow-on surfaces (persona picker, Voice|Text) can reuse it.

import { state, config, elements, endpoints, hooks } from './session.js';
import { addMessage, updateMessage, setStatus, buildSourcesHtml } from './thread.js';
import { clearAttachments } from './attachments.js';
import { loadConversations } from './conversations.js';
import { personaSupportsHandoff, personaOrchestrates } from './persona.js';
import { setStoredConversationId } from './backend.js';
import { startPendingQuestionPolling } from './pending-question.js';

// Low-level SSE transport. Builds the request body, opens the stream, and calls
// `on(data)` for each parsed `data:` event. If `on` returns `true`, processing
// stops immediately (used for the server-`error` path). `personaId`/`backend`
// are reserved for follow-ons and omitted from the body when unset, so today's
// request is byte-identical.
export async function askStream({ question, conversationId, attachments, personaId, backend, model, permissionGrants, permissionResume, on }) {
  const body = { question };
  if (conversationId) {
    body.conversation_id = conversationId;
  }
  if (permissionGrants && permissionGrants.length > 0) {
    body.permission_grants = permissionGrants;
  }
  if (permissionResume) {
    body.permission_resume = true;
  }

  // Include attachments if any
  if (attachments && attachments.length > 0) {
    body.attachments = attachments.map(att => ({
      filename: att.filename,
      media_type: att.mediaType,
      data: att.dataUrl.split(',')[1]  // Extract base64 data
    }));
  }

  // Both proxied backends are reached via their own endpoint (bearer added
  // server-side). Hermes resolves persona_id server-side into the
  // `lifeos_context` envelope, so it gets persona_id exactly like
  // lifeos does; the Agent backend still has no persona pass-through.
  // model_override stays lifeos-only — a persona's `model` frontmatter field
  // is a no-op on the other backends.
  const proxiedAskEndpoint = { agent: endpoints.agentAsk, hermes: endpoints.hermesAsk };
  // Orchestrating personas (e.g. doctor) run entirely on their selected
  // backend: Hermes drives its own background worker for these personas
  // (lifeos_agent_spawn), carrying a Hermes-specific preamble the proxy
  // attaches server-side (surface="hermes", hermes_proxy.py) — so a Hermes
  // turn is never special-cased here, orchestrating or not.
  const isLifeos = !proxiedAskEndpoint[backend];
  if (backend !== 'agent' && personaId != null) body.persona_id = personaId;
  // Per-turn model picker (lifeos backend only). 'auto' is the default — omit
  // it so the request stays byte-identical for users who never touch the picker.
  if (isLifeos && model && model !== 'auto') body.model_override = model;

  const response = await fetch(isLifeos ? endpoints.ask : proxiedAskEndpoint[backend], {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });

  if (!response.ok) {
    throw new Error('Request failed');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const chunk = decoder.decode(value);
    const lines = chunk.split('\n');

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try {
          const data = JSON.parse(line.slice(6));
          if (on(data) === true) return;  // handler asked to stop processing
        } catch (e) {
          // Skip malformed JSON
        }
      }
    }
  }
}

// Stop the turn in flight for the current conversation. A turn
// keeps running server-side after the browser gives up on the stream, so an
// explicit cancel is the only way to actually stop it — closing the tab or
// navigating away does not. Best-effort: on failure there's nothing
// useful to show the user; the turn just keeps running.
export async function stopTurn() {
  const conversationId = state.currentConversationId;
  if (!conversationId) return;
  try {
    await fetch(`${endpoints.conversations}/${encodeURIComponent(conversationId)}/cancel`, {
      method: 'POST',
    });
  } catch (e) {
    // network blip — nothing to surface here
  }
}

function permissionCard(request, onResume) {
  const card = document.createElement('div');
  card.className = 'permission-request';
  card.style.marginTop = '10px';
  card.style.padding = '10px 12px';
  card.style.border = '1px solid var(--border-color, #d0d0d0)';
  card.style.borderRadius = '10px';
  card.style.background = 'var(--bg-secondary, rgba(127,127,127,0.08))';

  const title = document.createElement('div');
  title.style.fontWeight = '600';
  title.textContent = `Permission required: ${request.service || 'protected service'}`;
  card.appendChild(title);

  const detail = document.createElement('div');
  detail.style.marginTop = '4px';
  detail.style.fontSize = '0.9em';
  detail.textContent = `${request.operation || 'access'} · ${request.scope || request.reason || ''}`;
  card.appendChild(detail);

  const controls = document.createElement('div');
  controls.style.display = 'flex';
  controls.style.gap = '8px';
  controls.style.marginTop = '10px';

  const allow = document.createElement('button');
  allow.type = 'button';
  allow.textContent = 'Allow once';
  const deny = document.createElement('button');
  deny.type = 'button';
  deny.textContent = 'Deny';
  for (const btn of [allow, deny]) {
    btn.style.padding = '6px 10px';
    btn.style.borderRadius = '8px';
    btn.style.border = '1px solid var(--border-color, #c8c8c8)';
    btn.style.cursor = 'pointer';
  }

  const setFinished = (text) => {
    allow.disabled = true;
    deny.disabled = true;
    allow.style.cursor = 'default';
    deny.style.cursor = 'default';
    const status = document.createElement('span');
    status.style.alignSelf = 'center';
    status.style.fontSize = '0.85em';
    status.textContent = text;
    controls.appendChild(status);
  };

  allow.addEventListener('click', async () => {
    if (allow.disabled) return;
    allow.disabled = true;
    deny.disabled = true;
    try {
      const response = await fetch(`${endpoints.permissions}/${encodeURIComponent(request.id)}/approve`, {
        method: 'POST',
        headers: { 'X-LifeOS-Permission-Approval': request.id },
      });
      if (!response.ok) throw new Error('Permission approval failed');
      setFinished('Approved for this one operation.');
      await onResume(request.id);
    } catch (error) {
      allow.disabled = false;
      deny.disabled = false;
      setStatus('error', 'Permission approval failed');
    }
  });

  deny.addEventListener('click', async () => {
    if (deny.disabled) return;
    allow.disabled = true;
    deny.disabled = true;
    try {
      const response = await fetch(`${endpoints.permissions}/${encodeURIComponent(request.id)}/deny`, {
        method: 'POST',
        headers: { 'X-LifeOS-Permission-Approval': request.id },
      });
      if (!response.ok) throw new Error('Permission denial failed');
      setFinished('Denied. No protected access was performed.');
    } catch (error) {
      allow.disabled = false;
      deny.disabled = false;
      setStatus('error', 'Permission denial failed');
    }
  });

  controls.appendChild(allow);
  controls.appendChild(deny);
  card.appendChild(controls);
  return card;
}

async function resumeWithPermission({ question, attachments, requestId }) {
  if (!state.currentConversationId) return;

  // A permission event is emitted just before the current turn closes. A fast
  // click can therefore land while the original SSE cleanup still owns the
  // composer. Wait briefly for that turn to release instead of approving a
  // grant and then silently abandoning the resume.
  for (let i = 0; state.isLoading && i < 100; i += 1) {
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  if (state.isLoading) {
    setStatus('error', 'Could not resume permission request');
    return;
  }

  state.isLoading = true;
  setStatus('loading', 'Continuing…');
  elements.sendBtn.disabled = true;
  elements.sendBtn.style.display = 'none';
  elements.stopBtn.classList.add('visible');

  const msgId = 'msg-' + Date.now();
  const msg = addMessage('', 'assistant', [], msgId);
  msg.querySelector('.message-content').innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';

  let fullContent = '';
  let sources = [];
  let routingSources = [];
  let serverError = false;

  try {
    await askStream({
      question,
      conversationId: state.currentConversationId,
      attachments,
      personaId: config.personaId,
      backend: config.backend,
      model: config.model,
      permissionGrants: [requestId],
      permissionResume: true,
      on: (data) => {
        if (data.type === 'routing') {
          routingSources = data.sources || [];
        } else if (data.type === 'self_correction') {
          fullContent = '';
          updateMessage(msgId, '');
        } else if (data.type === 'content') {
          fullContent += data.content;
          updateMessage(msgId, fullContent);
        } else if (data.type === 'permission_request') {
          msg.appendChild(permissionCard(data.request, (nextId) => resumeWithPermission({
            question, attachments, requestId: nextId,
          })));
        } else if (data.type === 'sources') {
          sources = data.sources;
        } else if (data.type === 'conversation_id') {
          state.currentConversationId = data.conversation_id;
          setStoredConversationId(data.conversation_id);
        } else if (data.type === 'usage') {
          const cost = data.cost_usd;
          if (typeof cost === 'number' && Number.isFinite(cost)) state.sessionCost += cost;
          else state.sessionCostUnpriced += 1;
          const prefix = state.sessionCostUnpriced > 0 ? '~' : '';
          elements.sessionCostEl.textContent = prefix + '$' + state.sessionCost.toFixed(3);
        } else if (data.type === 'done') {
          const msgEl = document.getElementById(msgId);
          if (msgEl) {
            let metaHtml = '';
            const sourceIcons = { vault: '📚', calendar: '📅', gmail: '✉️', drive: '📁', attachment: '📎', people: '👤', actions: '✅' };
            const dataSources = routingSources.filter(src => sourceIcons[src]);
            if (dataSources.length > 0) {
              metaHtml += '<div class="routing-sources">';
              dataSources.forEach(src => { metaHtml += `<span class="routing-source ${src}">${sourceIcons[src]} ${src}</span>`; });
              metaHtml += '</div>';
            }
            metaHtml += buildSourcesHtml(sources);
            if (metaHtml) msgEl.insertAdjacentHTML('beforeend', `<div class="message-meta">${metaHtml}</div>`);
          }
          loadConversations();
        } else if (data.type === 'error') {
          serverError = true;
          updateMessage(msgId, fullContent || 'Sorry, I could not continue after permission approval.');
          setStatus('error', 'Error');
          return true;
        }
      },
    });
    if (!serverError) setStatus('', 'Ready');
  } catch (error) {
    console.error('Permission resume failed:', error);
    updateMessage(msgId, fullContent || 'Sorry, I could not continue after permission approval.');
    setStatus('error', 'Error');
  } finally {
    state.isLoading = false;
    elements.sendBtn.disabled = false;
    elements.sendBtn.style.display = '';
    elements.stopBtn.classList.remove('visible');
    elements.inputField.focus();
  }
}

export async function sendMessage() {
  const question = elements.inputField.value.trim();
  if (!question || state.isLoading) return;

  // In agent-thread mode the composer continues that thread instead
  // of starting a normal chat query.
  if (state.currentAgentThread) {
    await hooks.onAgentThreadReply(question);
    return;
  }

  state.isLoading = true;
  setStatus('loading', 'Thinking...');
  elements.sendBtn.disabled = true;
  // Swap Send for Stop — hidden again once this turn settles, in the
  // same place sendBtn is re-enabled below.
  elements.sendBtn.style.display = 'none';
  elements.stopBtn.classList.add('visible');

  // Capture attachments before clearing
  const messageAttachments = [...state.attachments];
  const attachmentCount = messageAttachments.length;

  // Add user message with attachment indicator
  let userMsgContent = question;
  if (attachmentCount > 0) {
    userMsgContent += `\n<span class="attachment-indicator">📎 ${attachmentCount} attachment${attachmentCount > 1 ? 's' : ''}</span>`;
  }
  addMessage(userMsgContent, 'user');
  elements.inputField.value = '';
  elements.inputField.style.height = 'auto';

  // Clear attachments after capturing them
  clearAttachments();

  // Update title if new conversation
  if (!state.currentConversationId) {
    elements.chatTitle.textContent = question.slice(0, 40) + (question.length > 40 ? '...' : '');
  }

  // Add placeholder for assistant response
  const msgId = 'msg-' + Date.now();
  const msg = addMessage('', 'assistant', [], msgId);

  // Add typing indicator
  const typingHtml = '<div class="typing"><span></span><span></span><span></span></div>';
  msg.querySelector('.message-content').innerHTML = typingHtml;

  let fullContent = '';
  let sources = [];
  let routingSources = [];  // Track which sources were used
  // A server `error` event marks the turn failed while the shared cleanup
  // below still releases the composer for a retry.
  let serverError = false;

  try {
    await askStream({
      question,
      conversationId: state.currentConversationId,
      attachments: messageAttachments,
      personaId: config.personaId,
      backend: config.backend,
      model: config.model,
      on: (data) => {
        if (data.type === 'routing') {
          // Capture which sources are being used
          routingSources = data.sources || [];
          console.log('Routing to:', routingSources);
        } else if (data.type === 'self_correction') {
          fullContent = '';
          updateMessage(msgId, '');
        } else if (data.type === 'content') {
          fullContent += data.content;
          updateMessage(msgId, fullContent);
        } else if (data.type === 'permission_request') {
          msg.appendChild(permissionCard(data.request, (requestId) => resumeWithPermission({
            question, attachments: messageAttachments, requestId,
          })));
        } else if (data.type === 'sources') {
          sources = data.sources;
        } else if (data.type === 'conversation_id') {
          state.currentConversationId = data.conversation_id;
          setStoredConversationId(data.conversation_id);  // per-backend persistence
        } else if (data.type === 'usage') {
          // A backend that can't price a turn sends no `cost_usd`
          // rather than inventing a zero. `data.cost_usd || 0` treated an
          // absent cost the same as a real one, silently turning "unknown"
          // into a confident (wrong) claim of "free". An explicit
          // presence-and-type check keeps the two apart -- `0` still takes
          // this branch and adds nothing, same as before, but leaves no
          // mark; anything else (missing, null, a string) adds nothing and
          // marks the total as a lower bound instead.
          const cost = data.cost_usd;
          if (typeof cost === 'number' && Number.isFinite(cost)) {
            state.sessionCost += cost;
          } else {
            state.sessionCostUnpriced += 1;
          }
          const prefix = state.sessionCostUnpriced > 0 ? '~' : '';
          elements.sessionCostEl.textContent = prefix + '$' + state.sessionCost.toFixed(3);
          elements.sessionCostEl.title = state.sessionCostUnpriced > 0
            ? state.sessionCostUnpriced + ' turn(s) this session had no reported cost -- total is a lower bound.'
            : '';
        } else if (data.type === 'claude_intent') {
          // Repository-capable worker handoffs are protected by the same
          // permission-first rule as Gmail/Calendar/etc. The spawn endpoint
          // itself returns HTTP 428 with a permission request until a one-use
          // grant is approved, so a client cannot bypass the gate by calling
          // the handoff route directly.
          if (personaSupportsHandoff() || config.model === 'claude_code' || data.execution) {
            const engine = data.engine || 'claude_code';
            const label = engine === 'codex' ? 'Codex' : 'Claude Code';
            fullContent = 'Preparing ' + label + ' handoff…';
            updateMessage(msgId, fullContent);
            const handoffConversationId = state.currentConversationId;
            const payload = {
              engine: engine,
              task: data.task || '',
              conversation_id: handoffConversationId,
              ...(data.execution ? { execution: data.execution } : {}),
            };

            const startHandoff = async (permissionGrantId = null) => {
              try {
                const response = await fetch(endpoints.handoff, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({
                    ...payload,
                    ...(permissionGrantId ? { permission_grant_id: permissionGrantId } : {}),
                  }),
                });
                const d = await response.json().catch(() => ({}));
                if (response.status === 428
                    && d && d.detail && d.detail.code === 'permission_required'
                    && d.detail.request) {
                  fullContent = `I need your permission before ${label} can access the repository for this request.`;
                  updateMessage(msgId, fullContent);
                  msg.appendChild(permissionCard(d.detail.request, (requestId) => startHandoff(requestId)));
                  return;
                }
                if (!response.ok) throw new Error((d && d.detail) || 'handoff failed');
                fullContent = (d && d.message)
                  ? d.message
                  : '⚠️ Handoff to ' + label + ' failed.';
                updateMessage(msgId, fullContent);
                if (d && d.ok && handoffConversationId) {
                  startPendingQuestionPolling(handoffConversationId);
                }
              } catch (error) {
                fullContent = '⚠️ Handoff to ' + label + ' failed.';
                updateMessage(msgId, fullContent);
              }
            };
            startHandoff();
          }
        } else if (data.type === 'done') {
          // Add sources and meta to message
          const msgEl = document.getElementById(msgId);
          if (msgEl) {
            let metaHtml = '';

            // Show routing sources used — but only genuine data sources. The
            // `routing` event also carries internal plumbing labels (agent,
            // claude_code, clarification, escalation model names, codex) that
            // aren't "sources" to the user; those get filtered out here so an
            // answer that pulled no data doesn't render a lone "agent" pill.
            const sourceIcons = {
              vault: '📚',
              calendar: '📅',
              gmail: '✉️',
              drive: '📁',
              attachment: '📎',
              people: '👤',
              actions: '✅'
            };
            const dataSources = routingSources.filter(src => sourceIcons[src]);
            if (dataSources.length > 0) {
              metaHtml += '<div class="routing-sources">';
              dataSources.forEach(src => {
                metaHtml += `<span class="routing-source ${src}">${sourceIcons[src]} ${src}</span>`;
              });
              metaHtml += '</div>';
            }

            metaHtml += buildSourcesHtml(sources);

            // Remove any existing meta
            const existingMeta = msgEl.querySelector('.message-meta');
            if (existingMeta) existingMeta.remove();

            // Only render the meta block when there's something to show
            // (routing sources or vault sources); no save-to-vault button.
            if (metaHtml) {
              msgEl.insertAdjacentHTML('beforeend', `<div class="message-meta">${metaHtml}</div>`);
            }
          }

          loadConversations();

          // Orchestrating-persona spawn: this turn started a
          // background Claude Code session (routed to `claude_code`, the
          // doctor spawn path) and the conversation is now linked to it. Begin
          // polling for a `[CLARIFY]`/`[GOAL]` so it can be answered here
          // without Telegram. Gated on BOTH the routing and the orchestrating
          // persona: `claude_code` is also emitted by plain handoffs (model
          // picker / inferred-terminal / escalation ladder) that never link a
          // session, and only an orchestrating persona's turn is a spawn — so
          // this excludes those handoffs and never polls on a normal turn.
          if (routingSources.includes('claude_code') && personaOrchestrates()
              && state.currentConversationId) {
            startPendingQuestionPolling(state.currentConversationId);
          }
        } else if (data.type === 'error') {
          // Handle error from server
          const errorMsg = data.message || 'An error occurred';
          let userMessage = 'Sorry, I encountered an error and could not complete this request.';

          // Provide helpful messages for common errors
          if (errorMsg.toLowerCase().includes('api') ||
            errorMsg.toLowerCase().includes('auth') ||
            errorMsg.toLowerCase().includes('key') ||
            errorMsg.toLowerCase().includes('token')) {
            userMessage = 'API configuration error. Please check that your API key is set correctly.';
          } else if (errorMsg.toLowerCase().includes('timeout')) {
            userMessage = 'The request timed out. Please try again.';
          } else if (errorMsg.toLowerCase().includes('rate')) {
            userMessage = 'Rate limit exceeded. Please wait a moment and try again.';
          }

          console.error('Stream error:', errorMsg);
          // Keep any content already rendered (and persisted by the server)
          // visible when the terminal error follows a partial response. Only
          // use the generic failure copy when no useful content arrived.
          if (fullContent) {
            updateMessage(msgId, fullContent);
          } else {
            updateMessage(msgId, userMessage);
          }
          setStatus('error', 'Error');
          serverError = true;
          return true; // Stop processing
        }
      },
    });

    if (!serverError) setStatus('', 'Ready');

  } catch (error) {
    console.error('Error:', error);
    updateMessage(msgId, 'Sorry, something went wrong. Please try again.');
    setStatus('error', 'Error');
  }

  state.isLoading = false;
  elements.sendBtn.disabled = false;
  elements.sendBtn.style.display = '';
  elements.stopBtn.classList.remove('visible');
  elements.inputField.focus();
}

export function askQuestion(question) {
  elements.inputField.value = question;
  sendMessage();
}
