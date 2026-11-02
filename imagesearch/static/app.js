const $ = id => document.getElementById(id);
let activeId = null;
let pollTimer = null;
let renderedImages = 0;
let renderedErrors = 0;
const terminal = new Set(['complete', 'partial', 'failed', 'cancelled']);
const keywords = () => [...new Set($('keywords').value.split(/\r?\n/).map(k => k.trim()).filter(Boolean))];

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map(x => x.msg).join('; ') : data.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return data;
}
function estimate() {
  const n = keywords().length, count = Number($('count').value) || 0;
  $('estimate').textContent = n ? `${n} keyword${n === 1 ? '' : 's'} × ${count} images = up to ${n * count} images` : `${count} images per keyword`;
}
$('keywords').addEventListener('input', estimate);
$('count').addEventListener('input', estimate);
$('license').addEventListener('change', () => {
  const filtered = $('license').value !== 'all';
  if (filtered) $('engine').value = 'duckduckgo';
  $('engine').disabled = filtered;
});
$('keyword-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 100000) { $('form-error').textContent = 'Please use a text file smaller than 100 KB.'; return; }
  $('keywords').value = await file.text();
  estimate();
});
function fileUrl(id, name) {
  return `/api/jobs/${id}/files/${name.split('/').map(encodeURIComponent).join('/')}`;
}
function sourceLink(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; }
  catch { return null; }
}
function render(job) {
  const changed = activeId !== job.id;
  activeId = job.id;
  localStorage.setItem('imagesearch-last-job', job.id);
  if (changed) {
    renderedImages = 0; renderedErrors = 0;
    $('gallery').replaceChildren(); $('errors').replaceChildren();
  }
  $('empty').hidden = true; $('job').hidden = false;
  const done = terminal.has(job.status);
  const video = job.kind === 'video';
  $('results-title').textContent = video ? 'Your video' : 'Your collection';
  $('download-unit').textContent = video ? 'videos downloaded' : 'images collected';
  $('start').disabled = !done;
  $('video-start').disabled = !done;
  $('cancel').hidden = done;
  $('badge').textContent = job.status.toUpperCase();
  $('downloaded').textContent = job.downloaded;
  $('target').textContent = job.target;
  $('progress').value = video ? job.progress : (job.target ? job.downloaded / job.target * 100 : 0);
  $('message').textContent = job.message;
  $('folder').textContent = job.folder;
  $('manifest').href = fileUrl(job.id, 'manifest.json');
  $('csv').href = fileUrl(job.id, 'sources.csv');
  $('csv').hidden = video;
  $('gallery').hidden = video;
  $('video-files').hidden = !video;
  $('video-files').replaceChildren(...(job.videos || []).map(item => {
    const link = document.createElement('a');
    link.href = fileUrl(job.id, item.file); link.download = item.file;
    link.textContent = `Save video: ${item.title} (${(item.size / 1048576).toFixed(1)} MB)`;
    return link;
  }));
  $('keyword-status').replaceChildren(...job.keywords.map(item => {
    const row = document.createElement('div'); row.className = 'keyword-row';
    for (const text of [item.keyword, `${item.downloaded}/${item.target}`, item.status]) {
      const span = document.createElement('span'); span.textContent = text; row.append(span);
    }
    return row;
  }));
  for (const item of job.images.slice(renderedImages)) {
    const figure = document.createElement('figure'); figure.className = 'image-card';
    const link = document.createElement('a'); link.href = fileUrl(job.id, item.file); link.target = '_blank'; link.rel = 'noopener';
    const img = document.createElement('img'); img.src = link.href; img.alt = item.title || item.keyword; img.loading = 'lazy';
    link.append(img);
    const caption = document.createElement('figcaption');
    const source = document.createElement('a'); source.textContent = `${item.width} × ${item.height} · ${item.keyword}`;
    source.title = `${item.title}\n${item.license}`;
    const sourceUrl = sourceLink(item.source_url);
    if (sourceUrl) { source.href = sourceUrl; source.target = '_blank'; source.rel = 'noopener noreferrer'; }
    caption.append(source); figure.append(link, caption); $('gallery').append(figure);
  }
  renderedImages = job.images.length;
  for (const item of job.errors.slice(renderedErrors)) {
    const li = document.createElement('li'); li.textContent = `${item.keyword}: ${item.message}`; $('errors').append(li);
  }
  renderedErrors = job.errors.length;
  $('error-details').hidden = !job.errors.length;
  $('error-count').textContent = job.errors.length;
  return done;
}
async function poll() {
  clearTimeout(pollTimer);
  try {
    const done = render(await api(`/api/jobs/${activeId}`));
    $('form-error').textContent = '';
    if (!done) pollTimer = setTimeout(poll, 1200);
  } catch (error) {
    $('form-error').textContent = `${error.message} Retrying in 5 seconds…`;
    pollTimer = setTimeout(poll, 5000);
  }
}
$('form').addEventListener('submit', async event => {
  event.preventDefault(); $('form-error').textContent = '';
  if (!keywords().length) { $('form-error').textContent = 'Enter at least one keyword.'; return; }
  const options = {keywords: keywords(), count: Number($('count').value), engine: $('engine').value,
    collection: $('collection').value || 'collection', license: $('license').value, safesearch: $('safesearch').value,
    min_width: Number($('min-width').value), min_height: Number($('min-height').value), max_mb: Number($('max-mb').value)};
  $('start').disabled = true; $('video-start').disabled = true;
  try { render(await api('/api/jobs', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(options)})); poll(); }
  catch (error) { $('form-error').textContent = error.message; $('start').disabled = false; $('video-start').disabled = false; }
});
$('video-form').addEventListener('submit', async event => {
  event.preventDefault(); $('video-error').textContent = '';
  $('start').disabled = true; $('video-start').disabled = true;
  try {
    render(await api('/api/video-jobs', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: $('video-url').value.trim(), quality: $('video-quality').value})}));
    poll();
  } catch (error) {
    $('video-error').textContent = error.message;
    $('start').disabled = false; $('video-start').disabled = false;
  }
});
$('cancel').addEventListener('click', async () => {
  try { const result = await api(`/api/jobs/${activeId}/cancel`, {method: 'POST'}); $('message').textContent = result.message; }
  catch (error) { $('form-error').textContent = error.message; }
});
$('open').addEventListener('click', async () => {
  try { await api(`/api/jobs/${activeId}/open-folder`, {method: 'POST'}); }
  catch (error) { $('form-error').textContent = error.message; }
});
async function init() {
  try {
    const config = await api('/api/config'); $('output-root').textContent = config.output_root;
    const jobs = await api('/api/jobs');
    const saved = localStorage.getItem('imagesearch-last-job');
    const job = jobs.find(j => !terminal.has(j.status)) || jobs.find(j => j.id === saved) || jobs[0];
    if (job) { activeId = null; render(await api(`/api/jobs/${job.id}`)); if (!terminal.has(job.status)) poll(); }
  } catch (error) { $('form-error').textContent = error.message; }
}
init();
