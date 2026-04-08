(function () {
  const canvas = document.getElementById("starfield");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  let w = 0, h = 0, dpr = window.devicePixelRatio || 1;
  let stars = [];

  function resize() {
    w = window.innerWidth;
    h = window.innerHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    initStars();
  }

  function initStars() {
    const count = Math.min(280, Math.floor((w * h) / 6000));
    stars = [];
    for (let i = 0; i < count; i++) {
      stars.push({
        x: Math.random() * w,
        y: Math.random() * h,
        z: Math.random() * 0.8 + 0.2,
        vx: (Math.random() - 0.5) * 0.04,
        vy: (Math.random() - 0.5) * 0.02,
        s: Math.random() * 1.4 + 0.2,
        tw: Math.random() * Math.PI * 2,
      });
    }
  }

  function tick(t) {
    ctx.clearRect(0, 0, w, h);
    for (const s of stars) {
      s.x += s.vx * (s.z + 0.5);
      s.y += s.vy * (s.z + 0.5);
      if (s.x < 0) s.x = w;
      if (s.x > w) s.x = 0;
      if (s.y < 0) s.y = h;
      if (s.y > h) s.y = 0;
      const a = 0.4 + Math.sin(t * 0.001 + s.tw) * 0.3;
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.s, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(200, 220, 255, ${a * s.z})`;
      ctx.fill();
    }
    requestAnimationFrame(tick);
  }

  window.addEventListener("resize", resize);
  resize();
  requestAnimationFrame(tick);
})();
