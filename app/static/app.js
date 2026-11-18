// Comportamentos das telas de postagem: contadores, área de arrastar e soltar e sugestão com IA.
(function () {
  // Contadores de caracteres
  document.querySelectorAll('[data-count-for]').forEach((el) => {
    const input = document.getElementById(el.dataset.countFor);
    if (!input) return;
    const update = () => { el.textContent = input.value.length; };
    input.addEventListener('input', update);
    update();
  });

  // Área de arrastar e soltar com prévia do vídeo
  document.querySelectorAll('[data-dropzone]').forEach((zone) => {
    const input = zone.querySelector('input[type=file]');
    const preview = zone.querySelector('.dz-preview');
    const info = zone.querySelector('.dz-info');
    const empty = zone.querySelector('.dz-empty');
    const show = () => {
      const file = input.files[0];
      if (!file) return;
      preview.src = URL.createObjectURL(file);
      preview.hidden = false;
      empty.hidden = true;
      info.hidden = false;
      info.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB · clique para trocar`;
      const title = document.getElementById('f-title');
      if (title && !title.value) {
        title.value = file.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').slice(0, 100);
        title.dispatchEvent(new Event('input'));
      }
    };
    input.addEventListener('change', show);
    ['dragenter', 'dragover'].forEach((ev) => zone.addEventListener(ev, (e) => {
      e.preventDefault(); zone.classList.add('over');
    }));
    ['dragleave', 'drop'].forEach((ev) => zone.addEventListener(ev, () => zone.classList.remove('over')));
    zone.addEventListener('drop', (e) => {
      e.preventDefault();
      if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; show(); }
    });
    // Clicar na prévia não deve abrir o seletor de arquivo a cada play/pause
    preview.addEventListener('click', (e) => e.preventDefault());
  });

  // Sugestão de título, descrição e tags com IA
  document.querySelectorAll('[data-ai-suggest]').forEach((btn) => {
    const form = btn.closest('form');
    const title = form.querySelector('#f-title');
    const desc = form.querySelector('#f-desc');
    const tags = form.querySelector('#f-tags');
    const box = form.querySelector('.ai-titles');
    const status = form.querySelector('.ai-status');

    const setStatus = (text, isError) => {
      status.hidden = !text;
      status.textContent = text || '';
      status.classList.toggle('error-text', !!isError);
    };

    btn.addEventListener('click', async () => {
      const channel = form.querySelector('[data-channel-select]')?.value
        || form.querySelector('input[name=channel_ids]:checked')?.value || '';
      const video = form.querySelector('.dz-preview:not([hidden])') || document.querySelector('video.preview');
      const isShort = video && video.videoHeight > video.videoWidth;

      btn.disabled = true;
      btn.classList.add('loading');
      setStatus('Pensando em títulos…');
      try {
        const res = await fetch('/ai/suggest', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ topic: title.value, notes: desc.value, channel_id: channel, is_short: !!isShort }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Falha ao gerar sugestões.');

        box.innerHTML = '';
        data.titles.forEach((t) => {
          const b = document.createElement('button');
          b.type = 'button';
          b.className = 'chip';
          b.textContent = t;
          b.addEventListener('click', () => {
            title.value = t;
            title.dispatchEvent(new Event('input'));
            box.querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === b));
          });
          box.appendChild(b);
        });
        box.hidden = false;
        if (!desc.value.trim() || confirm('Substituir a descrição atual pela sugerida?')) {
          desc.value = data.description;
          desc.dispatchEvent(new Event('input'));
        }
        tags.value = data.tags.join(', ');
        setStatus('Escolha um dos títulos acima. Revise tudo antes de publicar.');
      } catch (err) {
        setStatus(err.message, true);
      } finally {
        btn.disabled = false;
        btn.classList.remove('loading');
      }
    });
  });

  // Mostra só os campos das plataformas escolhidas (tags/visibilidade: YouTube; legenda: Instagram)
  document.querySelectorAll('[data-post-form]').forEach((form) => {
    const sync = () => {
      const picked = new Set();
      form.querySelectorAll('input[name=channel_ids]:checked').forEach((i) => picked.add(i.dataset.platform));
      const opt = form.querySelector('[data-channel-select]')?.selectedOptions[0];
      if (opt?.dataset.platform) picked.add(opt.dataset.platform);
      if (!picked.size) picked.add('youtube');
      form.querySelectorAll('[data-only]').forEach((el) => { el.hidden = !picked.has(el.dataset.only); });
    };
    form.addEventListener('change', (e) => {
      if (e.target.name === 'channel_ids' || e.target.matches('[data-channel-select]')) sync();
    });
    sync();
  });

  // Evita envio duplo dos formulários de postagem
  document.querySelectorAll('[data-post-form]').forEach((form) => {
    form.addEventListener('submit', (e) => {
      const submitter = e.submitter;
      if (submitter?.value === 'queue' && form.querySelector('input[name=channel_ids]')
          && !form.querySelector('input[name=channel_ids]:checked')) {
        e.preventDefault();
        alert('Escolha pelo menos um canal.');
        return;
      }
      setTimeout(() => form.querySelectorAll('button').forEach((b) => { b.disabled = true; }), 0);
      if (submitter) submitter.classList.add('loading');
    });
  });
})();
