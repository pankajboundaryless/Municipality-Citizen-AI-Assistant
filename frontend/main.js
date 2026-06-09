// ── Utilities ────────────────────────────────────────────────
function generateUUID() {
  return ([1e7] + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, (c) =>
    (c ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16)
  );
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function getFileExt(filename) {
  return (filename.split('.').pop() || 'FILE').toUpperCase().slice(0, 4);
}

// ── Auth ──────────────────────────────────────────────────────
const loginSection = document.getElementById('login-section');

async function initAuth() {
  let data;
  try {
    const res = await fetch('/auth/me');
    data = await res.json();
  } catch {
    // Network error — show login gate
    loginSection.classList.remove('hidden');
    return;
  }

  // Show auth error banner if redirected back with ?auth_error=1
  if (new URLSearchParams(location.search).get('auth_error')) {
    document.getElementById('auth-error-msg')?.classList.remove('hidden');
    history.replaceState({}, '', '/');
  }

  if (!data.authenticated) {
    // Show only the buttons for providers that are configured
    const msBtn = document.getElementById('loginMicrosoftBtn');
    const gBtn  = document.getElementById('loginGoogleBtn');
    const providers = data.providers || [];
    if (msBtn) msBtn.style.display = providers.includes('microsoft') ? '' : 'none';
    if (gBtn)  gBtn.style.display  = providers.includes('google')    ? '' : 'none';
    loginSection.classList.remove('hidden');
    return;
  }

  // User is authenticated — show the citizen form
  loginSection.classList.add('hidden');
  authSection.classList.remove('hidden');

  if (data.user) {
    // Pre-fill form fields from OAuth profile
    if (data.user.name && !citizenNameEl.value) {
      citizenNameEl.value = data.user.name;
      validateForm();
    }
    if (data.user.email && !citizenEmailEl.value) {
      citizenEmailEl.value = data.user.email;
    }

    // Check for a previous session to resume
    checkResumeSession();

    // Show user indicator in the header
    const indicator = document.getElementById('user-indicator');
    const nameEl    = document.getElementById('user-display-name');
    const avatarImg = document.getElementById('user-avatar-img');
    if (indicator) {
      if (nameEl) nameEl.textContent = data.user.name || data.user.email || 'Signed in';
      if (avatarImg && data.user.picture) {
        avatarImg.src = data.user.picture;
        avatarImg.style.display = 'inline-block';
      }
      indicator.classList.remove('hidden');
    }
  }
}

// ── DOM References ────────────────────────────────────────────
const authSection       = document.getElementById('auth-section');
const appSection        = document.getElementById('app-section');
const sessionEndSection = document.getElementById('session-end-section');
const connectBtn        = document.getElementById('connectBtn');
const restartBtn        = document.getElementById('restartBtn');
const micBtn            = document.getElementById('micBtn');
const cameraBtn         = document.getElementById('cameraBtn');
const captureBtn        = document.getElementById('captureBtn');
const screenBtn         = document.getElementById('screenBtn');
const disconnectBtn     = document.getElementById('disconnectBtn');
const textInput         = document.getElementById('textInput');
const sendBtn           = document.getElementById('sendBtn');
const chatLog           = document.getElementById('chat-log');
const fileInput         = document.getElementById('fileInput');
const uploadArea        = document.getElementById('upload-area');
const uploadedFilesEl   = document.getElementById('uploaded-files');
const assistantStatus   = document.getElementById('assistant-status');
const videoCard         = document.getElementById('video-card');
const videoPreview      = document.getElementById('video-preview');
const videoLabel        = document.getElementById('video-label');

const citizenNameEl  = document.getElementById('citizenName');
const citizenIdEl    = document.getElementById('citizenId');
const citizenEmailEl = document.getElementById('citizenEmail');
const citizenPhoneEl = document.getElementById('citizenPhone');
const serviceCards   = document.querySelectorAll('.service-card');

// ── State ─────────────────────────────────────────────────────
let currentSessionId  = null;
let selectedService   = '';
let detectedLanguage  = 'en';  // BCP-47 code from navigator.languages
let currentGeminiDiv  = null;
let currentUserDiv    = null;
let processingNotice  = null;  // reference to the active "Processing…" notice
const localFiles      = [];   // {name, size} tracking for UI

// ── Service Card Selection ────────────────────────────────────
serviceCards.forEach((card) => {
  card.addEventListener('click', () => {
    serviceCards.forEach((c) => {
      c.classList.remove('selected');
      c.setAttribute('aria-pressed', 'false');
    });
    card.classList.add('selected');
    card.setAttribute('aria-pressed', 'true');
    selectedService = card.dataset.service;
    validateForm();
  });
});

// ── Form Validation ───────────────────────────────────────────
function validateForm() {
  connectBtn.disabled = citizenNameEl.value.trim().length === 0;
}

citizenNameEl.addEventListener('input', validateForm);

// ── Media + WebSocket ─────────────────────────────────────────
const mediaHandler = new MediaHandler();

const geminiClient = new GeminiClient({
  onOpen: () => {
    setStatus('connected', 'Connected');
    authSection.classList.add('hidden');
    appSection.classList.remove('hidden');
    populateSidebar();

    const name    = citizenNameEl.value.trim() || 'Citizen';
    const service = selectedService || 'general inquiry';
    geminiClient.sendText(
      `System: The citizen ${name} has just connected. Their requested service is: ${service}. ` +
      `Their preferred language (detected from browser) is: ${detectedLanguage}. ` +
      `Respond in this language throughout the session (switch naturally if they use another). ` +
      `Greet them warmly by first name, briefly confirm you can assist with their ${service} enquiry, ` +
      `and ask one focused opening question to understand their specific need. Keep the greeting under 3 sentences.`
    );
  },

  onMessage: (event) => {
    if (typeof event.data === 'string') {
      try { handleJsonMessage(JSON.parse(event.data)); }
      catch (e) { console.error('Parse error:', e); }
    } else {
      mediaHandler.playAudio(event.data);
    }
  },

  onClose: () => {
    setStatus('disconnected', 'Not Connected');
    showSessionEnd();
  },

  onError: () => {
    setStatus('error', 'Connection Error');
  },
});

// ── Status ────────────────────────────────────────────────────
function setStatus(state, label) {
  const badge = document.getElementById('status');
  badge.className = `status-badge ${state}`;
  badge.querySelector('.status-text').textContent = label;
}

// ── Message Handling ──────────────────────────────────────────
function handleJsonMessage(msg) {
  switch (msg.type) {
    case 'interrupted':
      mediaHandler.stopAudioPlayback();
      currentGeminiDiv = null;
      currentUserDiv   = null;
      if (assistantStatus) assistantStatus.textContent = 'Ready to assist';
      break;

    case 'turn_complete':
      currentGeminiDiv = null;
      currentUserDiv   = null;
      if (assistantStatus) assistantStatus.textContent = 'Ready to assist';
      break;

    case 'user':
      if (currentUserDiv) {
        currentUserDiv.querySelector('.message-text').textContent += msg.text;
        scrollChat();
      } else {
        currentUserDiv = appendMessage('user', msg.text, { voice: true });
      }
      break;

    case 'gemini':
      removeProcessingNotice();
      if (assistantStatus) assistantStatus.textContent = 'Responding...';
      if (currentGeminiDiv) {
        currentGeminiDiv.querySelector('.message-text').textContent += msg.text;
        scrollChat();
      } else {
        currentGeminiDiv = appendMessage('assistant', msg.text);
      }
      break;

    case 'tool_start':
      currentGeminiDiv = null;
      if (assistantStatus) assistantStatus.textContent = 'Processing request...';
      processingNotice = appendProcessingNotice();
      break;

    case 'tool_call':
      removeProcessingNotice();
      break;

    case 'error':
      appendSystemMsg('An error occurred: ' + (msg.error || 'Unknown error.'));
      break;
  }
}

function appendMessage(type, text, opts = {}) {
  const wrapper = document.createElement('div');
  wrapper.className = `message-wrapper ${type}`;

  const bubble = document.createElement('div');
  bubble.className = `message-bubble ${type}`;

  const span = document.createElement('span');
  span.className = 'message-text';
  span.textContent = text;

  bubble.appendChild(span);

  if (opts.voice) {
    const voiceLabel = document.createElement('span');
    voiceLabel.className = 'message-voice-label';
    voiceLabel.setAttribute('aria-label', 'Voice input — transcribed automatically');
    voiceLabel.innerHTML =
      '<svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true">' +
        '<rect x="3" y="1" width="4" height="5" rx="2" stroke="currentColor" stroke-width="1.1"/>' +
        '<path d="M1.5 5.5c0 1.93 1.57 3.5 3.5 3.5s3.5-1.57 3.5-3.5" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>' +
        '<path d="M5 9v1" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>' +
      '</svg>' +
      ' Transcribed — may not be verbatim';
    bubble.appendChild(voiceLabel);
  }

  const time = document.createElement('span');
  time.className = 'message-time';
  time.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  bubble.appendChild(time);
  wrapper.appendChild(bubble);
  chatLog.appendChild(wrapper);
  scrollChat();
  return wrapper;
}

function appendSystemMsg(text) {
  const div = document.createElement('div');
  div.className = 'message-system';
  div.textContent = text;
  chatLog.appendChild(div);
  scrollChat();
}

function appendProcessingNotice() {
  const div = document.createElement('div');
  div.className = 'message-processing';
  div.innerHTML = `
    <span>Processing with municipal system</span>
    <div class="dot-flashing" aria-hidden="true">
      <span></span><span></span><span></span>
    </div>`;
  chatLog.appendChild(div);
  scrollChat();
  return div;
}

function removeProcessingNotice() {
  if (processingNotice && processingNotice.parentNode) {
    processingNotice.remove();
  }
  processingNotice = null;
}

function scrollChat() {
  chatLog.scrollTop = chatLog.scrollHeight;
}

// ── Sidebar Population ────────────────────────────────────────
function populateSidebar() {
  const name = citizenNameEl.value.trim();
  const initials = name.split(' ').map((n) => n[0]).join('').toUpperCase().slice(0, 2) || '?';

  document.getElementById('citizen-initials').textContent       = initials;
  document.getElementById('citizen-name-display').textContent   = name || '—';
  document.getElementById('service-badge').textContent          = selectedService || 'General Inquiry';
  document.getElementById('display-id').textContent             = citizenIdEl.value.trim() || '—';
  document.getElementById('display-email').textContent          = citizenEmailEl.value.trim() || '—';
  document.getElementById('display-phone').textContent          = citizenPhoneEl.value.trim() || '—';
  document.getElementById('display-session').textContent        = currentSessionId
    ? currentSessionId.slice(0, 8) + '…'
    : '—';

  document.getElementById('reference-number').textContent = currentSessionId || '—';
}

// ── Connect ───────────────────────────────────────────────────
connectBtn.addEventListener('click', async () => {
  if (!citizenNameEl.value.trim()) return;

  connectBtn.disabled = true;
  connectBtn.textContent = 'Connecting…';
  setStatus('connecting', 'Connecting…');

  try {
    currentSessionId = generateUUID();

    detectedLanguage = navigator.languages?.[0] || navigator.language || 'en';
    const citizenData = {
      name:              citizenNameEl.value.trim(),
      idNumber:          citizenIdEl.value.trim(),
      email:             citizenEmailEl.value.trim(),
      phone:             citizenPhoneEl.value.trim(),
      selectedService:   selectedService || 'General Inquiry',
      preferredLanguage: detectedLanguage,
    };

    const res = await fetch('/session/init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sessionId: currentSessionId,
        citizenData,
        transcript: currentResumeTranscript,
      }),
    });

    if (!res.ok) throw new Error('Session initialisation failed');

    await mediaHandler.initializeAudio();
    geminiClient.connect(currentSessionId);

  } catch (err) {
    console.error('Connection error:', err);
    setStatus('error', 'Connection Failed');
    connectBtn.disabled = false;
    connectBtn.textContent = 'Start Session';
    alert('Could not start session: ' + err.message);
  }
});

// ── Disconnect ────────────────────────────────────────────────
disconnectBtn.addEventListener('click', () => {
  geminiClient.disconnect();
});

// ── Mic Toggle ────────────────────────────────────────────────
micBtn.addEventListener('click', async () => {
  const label = micBtn.querySelector('.mic-label');

  if (mediaHandler.isRecording) {
    mediaHandler.stopAudio();
    label.textContent = 'Start Microphone';
    micBtn.setAttribute('aria-pressed', 'false');
    micBtn.classList.remove('active');
  } else {
    try {
      await mediaHandler.startAudio((data) => {
        if (geminiClient.isConnected()) geminiClient.send(data);
      });
      label.textContent = 'Stop Microphone';
      micBtn.setAttribute('aria-pressed', 'true');
      micBtn.classList.add('active');
    } catch {
      alert('Could not access the microphone. Please check your browser permissions.');
    }
  }
});

// ── Camera Toggle ─────────────────────────────────────────────
cameraBtn.addEventListener('click', async () => {
  const label = cameraBtn.querySelector('.camera-label');

  if (cameraBtn.getAttribute('aria-pressed') === 'true') {
    mediaHandler.stopVideo(videoPreview);
    videoCard.classList.add('hidden');
    captureBtn.classList.add('hidden');
    cameraBtn.setAttribute('aria-pressed', 'false');
    cameraBtn.classList.remove('active');
    label.textContent = 'Start Camera';
  } else {
    // Stop screen share first if active
    if (screenBtn.getAttribute('aria-pressed') === 'true') {
      mediaHandler.stopVideo(videoPreview);
      screenBtn.setAttribute('aria-pressed', 'false');
      screenBtn.classList.remove('active');
      screenBtn.querySelector('.screen-label').textContent = 'Share Screen';
    }
    try {
      await mediaHandler.startVideo(videoPreview, (base64Data) => {
        if (geminiClient.isConnected()) geminiClient.sendImage(base64Data);
      });
      videoLabel.textContent = 'Camera';
      videoCard.classList.remove('hidden');
      captureBtn.classList.remove('hidden');
      cameraBtn.setAttribute('aria-pressed', 'true');
      cameraBtn.classList.add('active');
      label.textContent = 'Stop Camera';
    } catch {
      alert('Could not access the camera. Please check your browser permissions.');
    }
  }
});

// ── Screen Share Toggle ───────────────────────────────────────
screenBtn.addEventListener('click', async () => {
  const label = screenBtn.querySelector('.screen-label');

  if (screenBtn.getAttribute('aria-pressed') === 'true') {
    mediaHandler.stopVideo(videoPreview);
    videoCard.classList.add('hidden');
    screenBtn.setAttribute('aria-pressed', 'false');
    screenBtn.classList.remove('active');
    label.textContent = 'Share Screen';
  } else {
    // Stop camera first if active
    if (cameraBtn.getAttribute('aria-pressed') === 'true') {
      mediaHandler.stopVideo(videoPreview);
      cameraBtn.setAttribute('aria-pressed', 'false');
      cameraBtn.classList.remove('active');
      cameraBtn.querySelector('.camera-label').textContent = 'Start Camera';
    }
    try {
      await mediaHandler.startScreen(
        videoPreview,
        (base64Data) => {
          if (geminiClient.isConnected()) geminiClient.sendImage(base64Data);
        },
        () => {
          // User stopped sharing via browser UI
          videoCard.classList.add('hidden');
          screenBtn.setAttribute('aria-pressed', 'false');
          screenBtn.classList.remove('active');
          label.textContent = 'Share Screen';
        }
      );
      videoLabel.textContent = 'Screen';
      videoCard.classList.remove('hidden');
      screenBtn.setAttribute('aria-pressed', 'true');
      screenBtn.classList.add('active');
      label.textContent = 'Stop Sharing';
    } catch {
      alert('Could not start screen sharing. Please check your browser permissions.');
    }
  }
});

// ── Capture Photo ─────────────────────────────────────────────
captureBtn.addEventListener('click', () => {
  if (!videoPreview.videoWidth || !geminiClient.isConnected()) return;

  // Draw the current video frame to an off-screen canvas
  const canvas = document.createElement('canvas');
  canvas.width  = videoPreview.videoWidth;
  canvas.height = videoPreview.videoHeight;
  canvas.getContext('2d').drawImage(videoPreview, 0, 0);

  const ts       = new Date().toTimeString().slice(0, 8).replace(/:/g, '');
  const filename = `captured_photo_${ts}.jpg`;
  const dataUrl  = canvas.toDataURL('image/jpeg', 0.92);
  const base64   = dataUrl.split(',')[1];

  // Send to backend for storage
  geminiClient.send(JSON.stringify({ type: 'capture_photo', data: base64, filename }));

  // Add to the local file list so the citizen can see it
  const approxBytes = Math.round(base64.length * 0.75);
  localFiles.push({ name: filename, size: approxBytes, captured: true });
  renderFileList();

  // Visual feedback: white flash on the video preview
  const wrapper = videoPreview.parentElement;
  wrapper.classList.add('capture-flash');
  wrapper.addEventListener('animationend', () => wrapper.classList.remove('capture-flash'), { once: true });

  appendSystemMsg(`Photo captured: ${filename}`);
});

// ── Text Chat ─────────────────────────────────────────────────
sendBtn.addEventListener('click', sendText);
textInput.addEventListener('keypress', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendText();
  }
});

function sendText() {
  const text = textInput.value.trim();
  if (!text || !geminiClient.isConnected()) return;
  geminiClient.sendText(text);
  appendMessage('user', text);
  textInput.value = '';
}

// ── Document Upload ───────────────────────────────────────────
uploadArea.addEventListener('click', () => fileInput.click());

uploadArea.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    fileInput.click();
  }
});

uploadArea.addEventListener('dragover', (e) => {
  e.preventDefault();
  uploadArea.classList.add('drag-over');
});

uploadArea.addEventListener('dragleave', () => {
  uploadArea.classList.remove('drag-over');
});

uploadArea.addEventListener('drop', (e) => {
  e.preventDefault();
  uploadArea.classList.remove('drag-over');
  processFiles(e.dataTransfer.files);
});

fileInput.addEventListener('change', () => {
  processFiles(fileInput.files);
  fileInput.value = '';
});

async function processFiles(files) {
  for (const file of Array.from(files)) {
    if (file.size > 10 * 1024 * 1024) {
      appendSystemMsg(`"${file.name}" exceeds the 10 MB limit and was not uploaded.`);
      continue;
    }
    await uploadFile(file);
  }
}

async function uploadFile(file) {
  const form = new FormData();
  form.append('sessionId', currentSessionId);
  form.append('file', file);

  try {
    const res = await fetch('/upload-document', { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      throw new Error(err.error || res.statusText);
    }

    localFiles.push({ name: file.name, size: file.size });
    renderFileList();

    // Notify the AI about the new document
    if (geminiClient.isConnected()) {
      geminiClient.send(
        JSON.stringify({ type: 'document_notify', filename: file.name, contentType: file.type })
      );
    }

    appendSystemMsg(`Document attached: ${file.name}`);
  } catch (err) {
    appendSystemMsg(`Failed to upload "${file.name}": ${err.message}`);
  }
}

function renderFileList() {
  uploadedFilesEl.innerHTML = '';
  localFiles.forEach((f, idx) => {
    const item = document.createElement('div');
    item.className = 'file-item';
    item.setAttribute('role', 'listitem');

    const ext = document.createElement('span');
    ext.className = f.captured ? 'file-ext file-ext-capture' : 'file-ext';
    ext.textContent = f.captured ? 'CAM' : getFileExt(f.name);

    const info = document.createElement('div');
    info.className = 'file-info';

    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = f.name;

    const size = document.createElement('span');
    size.className = 'file-size';
    size.textContent = formatBytes(f.size);

    info.append(name, size);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'file-delete';
    del.textContent = '×';
    del.setAttribute('aria-label', `Remove ${f.name}`);
    del.addEventListener('click', () => removeFile(idx, f.name));

    item.append(ext, info, del);
    uploadedFilesEl.appendChild(item);
  });
}

async function removeFile(idx, filename) {
  localFiles.splice(idx, 1);
  renderFileList();

  if (currentSessionId) {
    await fetch(
      `/upload-document?sessionId=${encodeURIComponent(currentSessionId)}&filename=${encodeURIComponent(filename)}`,
      { method: 'DELETE' }
    );
  }
}

// ── Video teardown helper ─────────────────────────────────────
function stopAllVideo() {
  if (mediaHandler.videoStream) {
    mediaHandler.stopVideo(videoPreview);
    videoCard.classList.add('hidden');
  }
  captureBtn.classList.add('hidden');
  cameraBtn.setAttribute('aria-pressed', 'false');
  cameraBtn.classList.remove('active');
  cameraBtn.querySelector('.camera-label').textContent = 'Start Camera';
  screenBtn.setAttribute('aria-pressed', 'false');
  screenBtn.classList.remove('active');
  screenBtn.querySelector('.screen-label').textContent = 'Share Screen';
}

// ── Session End / Reset ───────────────────────────────────────
function showSessionEnd() {
  appSection.classList.add('hidden');
  sessionEndSection.classList.remove('hidden');
  mediaHandler.stopAudio();
  stopAllVideo();

  const label = micBtn.querySelector('.mic-label');
  if (label) label.textContent = 'Start Microphone';
  micBtn.setAttribute('aria-pressed', 'false');
  micBtn.classList.remove('active');
}

function resetUI() {
  loginSection.classList.add('hidden');
  authSection.classList.remove('hidden');
  appSection.classList.add('hidden');
  sessionEndSection.classList.add('hidden');

  mediaHandler.stopAudio();
  stopAllVideo();

  chatLog.innerHTML = '';
  localFiles.length = 0;
  renderFileList();

  currentSessionId = null;
  selectedService  = '';
  currentGeminiDiv = null;
  currentUserDiv   = null;

  serviceCards.forEach((c) => {
    c.classList.remove('selected');
    c.setAttribute('aria-pressed', 'false');
  });

  citizenNameEl.value  = '';
  citizenIdEl.value    = '';
  citizenEmailEl.value = '';
  citizenPhoneEl.value = '';

  connectBtn.disabled    = true;
  connectBtn.textContent = 'Start Session';
  setStatus('disconnected', 'Not Connected');
}

restartBtn.addEventListener('click', resetUI);

// ── Session Resume ────────────────────────────────────────────
let _resumeData = null;

async function checkResumeSession() {
  try {
    const res = await fetch('/session/last');
    const data = await res.json();
    if (!data.found) return;

    _resumeData = data;
    const banner = document.getElementById('resume-banner');
    const info   = document.getElementById('resume-info');
    if (!banner) return;

    const date = new Date(data.updatedAt).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    });
    const service = data.citizenData?.selectedService || 'General Inquiry';
    if (info) info.textContent = `${service} — last active ${date}`;
    banner.classList.remove('hidden');
  } catch { /* ignore */ }
}

function applyResumeSession() {
  if (!_resumeData) return;
  const cd = _resumeData.citizenData || {};
  if (cd.name)  { citizenNameEl.value  = cd.name;  validateForm(); }
  if (cd.email) { citizenEmailEl.value = cd.email; }
  if (cd.phone) { citizenPhoneEl.value = cd.phone; }
  if (cd.idNumber) { citizenIdEl.value = cd.idNumber; }

  // Select the matching service card
  if (cd.selectedService) {
    serviceCards.forEach(c => {
      const match = c.dataset.service === cd.selectedService;
      c.classList.toggle('selected', match);
      c.setAttribute('aria-pressed', match ? 'true' : 'false');
      if (match) selectedService = cd.selectedService;
    });
    validateForm();
  }

  // Carry transcript so Gemini gets the context on reconnect
  currentResumeTranscript = _resumeData.transcript || [];
  document.getElementById('resume-banner')?.classList.add('hidden');
}

let currentResumeTranscript = [];

// Override connectBtn to pass transcript when resuming
const _origConnect = connectBtn.onclick;
connectBtn.addEventListener('click', async () => {
  // inject transcript into session init payload (handled in the existing click listener)
}, true);

// ── ID Card Scanner ───────────────────────────────────────────
// ── ID Card Scanner (clean rewrite) ──────────────────────────
let _scanStream    = null;
let _scanExtracted = null;

function _scanStep(name) {
  ['camera','processing','results','error'].forEach(s =>
    document.getElementById(`scanner-step-${s}`).classList.toggle('hidden', s !== name)
  );
}

function _playTone(freq, dur) {
  try {
    const ac = new (window.AudioContext || window.webkitAudioContext)();
    const o = ac.createOscillator(), g = ac.createGain();
    o.connect(g); g.connect(ac.destination);
    o.frequency.value = freq;
    g.gain.setValueAtTime(0.25, ac.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ac.currentTime + dur);
    o.start(); o.stop(ac.currentTime + dur);
  } catch { /**/ }
}
function _playShutter() { _playTone(1100, 0.06); setTimeout(() => _playTone(700, 0.09), 60); }
function _playSuccess()  { _playTone(660, 0.12); setTimeout(() => _playTone(880, 0.18), 130); }

let _cameraReady = false;

async function openScanner() {
  _cameraReady = false;
  document.getElementById('scanner-modal').classList.remove('hidden');
  _scanStep('camera');
  const badge = document.getElementById('scanner-status-badge');
  badge.textContent = '⏳ Starting camera…';
  badge.style.color = '#94a3b8';

  try {
    _scanStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } }
    });
  } catch {
    try { _scanStream = await navigator.mediaDevices.getUserMedia({ video: true }); }
    catch { _showScanError('Camera access denied — please allow camera permission and try again.'); return; }
  }

  const video = document.getElementById('scanner-video');
  video.srcObject = _scanStream;

  // Wait for first frame before allowing capture
  video.onloadeddata = () => {
    _cameraReady = true;
    badge.textContent = '📸 Tap the image to capture';
    badge.style.color = '#4ade80';
  };
}

function closeScanner() {
  _cameraReady = false;
  document.getElementById('scanner-modal').classList.add('hidden');
  if (_scanStream) { _scanStream.getTracks().forEach(t => t.stop()); _scanStream = null; }
  document.getElementById('scanner-video').srcObject = null;
}

function _showScanError(msg) {
  _scanStep('error');
  document.getElementById('scanner-error-msg').textContent =
    msg || "Couldn't read card clearly — please retake the photo.";
}

async function _doCapture() {
  const badge = document.getElementById('scanner-status-badge');

  if (!_scanStream) { _showScanError('Camera not ready — please reopen the scanner.'); return; }
  if (!_cameraReady) {
    badge.textContent = '⏳ Camera still starting — please wait a moment';
    badge.style.color = '#fbbf24';
    return;
  }

  _playShutter();
  _scanStep('processing');
  document.querySelector('.scan-processing-text').textContent = 'Scanning your document…';

  const video  = document.getElementById('scanner-video');
  const canvas = document.getElementById('scanner-canvas');
  canvas.width  = video.videoWidth  || 1280;
  canvas.height = video.videoHeight || 720;
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  const imageB64 = canvas.toDataURL('image/jpeg', 0.95).split(',')[1];

  // Debug: verify image captured is not blank
  const sizeKB = Math.round(imageB64.length * 0.75 / 1024);
  console.log(`[Scan] Image captured: ${canvas.width}x${canvas.height}, ~${sizeKB}KB`);

  if (sizeKB < 5) {
    _showScanError("Camera frame not ready — please wait for the badge to turn green then tap again.");
    return;
  }

  try {
    const res  = await fetch('/scan-id', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image: imageB64 }),
    });

    if (res.status === 401) {
      _showScanError('Session expired — please sign in again and retry.');
      return;
    }
    if (!res.ok) {
      _showScanError(`Server error (${res.status}) — please try again.`);
      return;
    }

    const json = await res.json();
    console.log('[Scan] API response:', JSON.stringify(json));

    if (!json.success) {
      _showScanError(json.error || "Card scan failed — please try again.");
      return;
    }

    // Accept if ANY meaningful field was extracted
    const meaningful = ['name','id_number','date_of_birth','nationality','expiry_date','document_type'];
    const hasData = json.data &&
      meaningful.some(k => json.data[k] && String(json.data[k]).toLowerCase() !== 'null');

    if (!hasData) {
      _showScanError("Card text not detected — ensure the card is well-lit, flat, and fills the frame, then tap again.");
      return;
    }

    _scanExtracted = json.data;
    _renderScanResults(json.data);
    _scanStep('results');
    _playSuccess();
  } catch (err) {
    console.error('[Scan] error:', err);
    _showScanError('Connection error — please check your network and try again.');
  }
}

function _renderScanResults(data) {
  const labels = {
    name: 'Full Name', id_number: 'ID / Permit No.',
    date_of_birth: 'Date of Birth', nationality: 'Nationality',
    expiry_date: 'Expiry Date', document_type: 'Document Type',
    gender: 'Gender', place_of_birth: 'Place of Birth', address: 'Address',
  };
  document.getElementById('scan-results').innerHTML =
    Object.entries(data).filter(([, v]) => v)
      .map(([k, v]) => `<div class="scan-field">
        <span class="scan-field-label">${labels[k] || k}</span>
        <span class="scan-field-value">${v}</span>
      </div>`).join('');
}

function _applyScan() {
  if (!_scanExtracted) return;
  const d = _scanExtracted;
  if (d.name)      { citizenNameEl.value = d.name; validateForm(); }
  if (d.id_number) { citizenIdEl.value   = d.id_number; }
  closeScanner();
  [citizenNameEl, citizenIdEl].forEach(el => {
    if (el.value) {
      el.classList.add('field-scanned');
      setTimeout(() => el.classList.remove('field-scanned'), 2000);
    }
  });
}

// ── Wire up via event delegation on the modal (works even when hidden) ──
document.getElementById('scanner-modal')?.addEventListener('click', e => {
  const t = e.target;
  // Tap video / viewport area → capture
  if (t.closest('.scanner-viewport')) { _doCapture(); return; }
  // Buttons
  if (t.id === 'scanIdBtn' || t.closest('#scanIdBtn'))             { openScanner(); return; }
  if (t.id === 'scannerCloseBtn'  || t.closest('#scannerCloseBtn'))  { closeScanner(); return; }
  if (t.id === 'scannerCancelBtn' || t.closest('#scannerCancelBtn')) { closeScanner(); return; }
  if (t.id === 'captureBtn'       || t.closest('#captureBtn'))       { _doCapture(); return; }
  if (t.id === 'scanUseBtn'       || t.closest('#scanUseBtn'))       { _applyScan(); return; }
  if (t.id === 'scanRetryBtn'     || t.closest('#scanRetryBtn'))     { _scanStep('camera'); return; }
  if (t.id === 'scanErrorRetryBtn'|| t.closest('#scanErrorRetryBtn')){ _scanStep('camera'); return; }
  if (t.id === 'scanErrorCancelBtn'||t.closest('#scanErrorCancelBtn')){ closeScanner(); return; }
  // Backdrop
  if (t.id === 'scanner-modal') closeScanner();
});

document.getElementById('scanIdBtn')?.addEventListener('click', openScanner);

// ── Initialise ────────────────────────────────────────────────
initAuth();
