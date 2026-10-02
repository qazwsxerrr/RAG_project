<template>
  <div class="fixed inset-0 pointer-events-none -z-10 overflow-hidden select-none">
    <!-- 浅色高科技空气感渐变基底 -->
    <div class="absolute inset-0 bg-gradient-to-b from-[#FAFBFD] via-[#F4F7FD] to-[#EDF2FA]"></div>

    <!-- 顶部微光柔和光晕 -->
    <div class="absolute -top-32 left-1/3 w-[800px] h-[400px] bg-gradient-to-b from-indigo-100/40 via-blue-50/20 to-transparent rounded-full blur-3xl"></div>
    <div class="absolute -bottom-32 right-1/4 w-[600px] h-[400px] bg-gradient-to-t from-sky-100/40 via-indigo-50/20 to-transparent rounded-full blur-3xl"></div>

    <!-- 极轻柔的微网格粒子画布 (对齐 image_eg 原生视觉) -->
    <canvas ref="canvasRef" class="absolute inset-0 w-full h-full opacity-65"></canvas>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue';

const canvasRef = ref<HTMLCanvasElement | null>(null);
let animationFrameId: number | null = null;

interface Particle {
  x: number;
  y: number;
  vx: number;
  vy: number;
  radius: number;
  color: string;
}

onMounted(() => {
  const canvas = canvasRef.value;
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  let width = (canvas.width = window.innerWidth);
  let height = (canvas.height = window.innerHeight);

  const handleResize = () => {
    if (!canvas) return;
    width = canvas.width = window.innerWidth;
    height = canvas.height = window.innerHeight;
  };
  window.addEventListener('resize', handleResize);

  // 初始化柔和科技粒子节点
  const particleCount = Math.min(Math.floor((width * height) / 28000), 55);
  const particles: Particle[] = [];
  const palette = [
    'rgba(99, 102, 241, 0.45)',  // Indigo
    'rgba(139, 92, 246, 0.4)',   // Violet
    'rgba(59, 130, 246, 0.4)',   // Blue
    'rgba(14, 165, 233, 0.35)',  // Sky
  ];

  for (let i = 0; i < particleCount; i++) {
    particles.push({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.35,
      vy: (Math.random() - 0.5) * 0.35,
      radius: Math.random() * 1.5 + 1.2,
      color: palette[Math.floor(Math.random() * palette.length)],
    });
  }

  const maxDist = 130;

  const render = () => {
    ctx.clearRect(0, 0, width, height);

    // 绘制连线
    for (let i = 0; i < particles.length; i++) {
      for (let j = i + 1; j < particles.length; j++) {
        const dx = particles[i].x - particles[j].x;
        const dy = particles[i].y - particles[j].y;
        const dist = Math.sqrt(dx * dx + dy * dy);

        if (dist < maxDist) {
          const alpha = (1 - dist / maxDist) * 0.22;
          ctx.beginPath();
          ctx.moveTo(particles[i].x, particles[i].y);
          ctx.lineTo(particles[j].x, particles[j].y);
          ctx.strokeStyle = `rgba(165, 180, 252, ${alpha})`;
          ctx.lineWidth = 0.75;
          ctx.stroke();
        }
      }
    }

    // 绘制粒子
    for (const p of particles) {
      p.x += p.vx;
      p.y += p.vy;

      if (p.x < 0) p.x = width;
      if (p.x > width) p.x = 0;
      if (p.y < 0) p.y = height;
      if (p.y > height) p.y = 0;

      ctx.beginPath();
      ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
      ctx.fillStyle = p.color;
      ctx.fill();
    }

    animationFrameId = requestAnimationFrame(render);
  };

  render();

  onUnmounted(() => {
    window.removeEventListener('resize', handleResize);
    if (animationFrameId) {
      cancelAnimationFrame(animationFrameId);
    }
  });
});
</script>
