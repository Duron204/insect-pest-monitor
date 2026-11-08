<script setup lang="ts">
/**
 * 登录页品牌插画：田间监测场景（纯 SVG + CSS 动画）。
 *
 * 为什么不用照片：实景图与令牌色系天然冲突（照片有独立色温），
 * 且无法随深浅主题联动；这里的每一笔颜色都取自 --ip-*，
 * 主题切换时整幅画自动换装。动画刻意只留两处：
 * 扫描线（"监测"的产品隐喻）与指示灯呼吸（设备在线）——
 * 多一处都会从"氛围"变成"干扰"。
 */
</script>

<template>
  <div class="hero-art" role="img" aria-label="田间监测场景插画：作物行与自动扫描线">
    <svg viewBox="0 0 400 220" class="hero-art__svg" aria-hidden="true">
      <defs>
        <linearGradient id="hero-sky" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="var(--ip-primary)" stop-opacity="0.16" />
          <stop offset="1" stop-color="var(--ip-primary)" stop-opacity="0.02" />
        </linearGradient>
        <linearGradient id="hero-scan" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="var(--ip-primary)" stop-opacity="0" />
          <stop offset="0.5" stop-color="var(--ip-primary)" stop-opacity="0.9" />
          <stop offset="1" stop-color="var(--ip-primary)" stop-opacity="0" />
        </linearGradient>
        <radialGradient id="hero-sun" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0" stop-color="var(--ip-primary)" stop-opacity="0.55" />
          <stop offset="1" stop-color="var(--ip-primary)" stop-opacity="0" />
        </radialGradient>
      </defs>

      <!-- 背景天空 -->
      <rect x="0" y="0" width="400" height="130" fill="url(#hero-sky)" />

      <!-- 朝阳光晕 -->
      <circle cx="322" cy="38" r="46" fill="url(#hero-sun)" />

      <!-- 远山轮廓 -->
      <path d="M0 122 Q 60 96 130 112 T 260 108 T 400 120 V 130 H0 Z" fill="var(--ip-primary)" opacity="0.08" />

      <!-- 田间作物行（透视收缩的地垄） -->
      <g stroke-linecap="round" fill="none">
        <path d="M60 130 C 140 128, 260 124, 340 122" stroke="var(--ip-primary)" stroke-width="2" opacity="0.30" />
        <path d="M28 148 C 130 146, 270 138, 372 132" stroke="var(--ip-primary)" stroke-width="2.6" opacity="0.38" />
        <path d="M-6 168 C 120 166, 280 154, 404 144" stroke="var(--ip-primary)" stroke-width="3.2" opacity="0.46" />
        <path d="M-40 190 C 110 190, 292 172, 436 158" stroke="var(--ip-primary)" stroke-width="3.8" opacity="0.55" />
        <path d="M-74 214 C 100 216, 304 194, 468 174" stroke="var(--ip-primary)" stroke-width="4.4" opacity="0.65" />
      </g>

      <!-- 垄间"检出"脉冲点 -->
      <circle class="hero-art__pulse" cx="150" cy="140" r="3.4" fill="var(--ip-primary)" />
      <circle class="hero-art__pulse hero-art__pulse--late" cx="252" cy="158" r="4.2" fill="var(--ip-primary)" />
      <circle class="hero-art__pulse hero-art__pulse--later" cx="96" cy="182" r="4.8" fill="var(--ip-primary)" />

      <!-- 监测立杆（右前）：杆 + 摄像头 + 天线 + 在线指示灯 -->
      <g class="hero-art__post">
        <line x1="318" y1="78" x2="318" y2="200" stroke="var(--ip-line-strong)" stroke-width="3.5" />
        <rect x="296" y="64" width="30" height="18" rx="4" fill="var(--ip-panel-3)" stroke="var(--ip-line-strong)" stroke-width="1.5" />
        <circle cx="324" cy="73" r="4.5" fill="var(--ip-primary)" opacity="0.9" />
        <line x1="306" y1="64" x2="306" y2="50" stroke="var(--ip-line-strong)" stroke-width="2" />
        <circle cx="306" cy="47" r="2.6" fill="var(--ip-primary)" />
        <circle class="hero-art__led" cx="303" cy="70" r="2.2" fill="var(--ip-ok)" />
      </g>

      <!-- 扫描线：横向往复扫过整片田（监测的产品隐喻） -->
      <g class="hero-art__scan">
        <rect x="-14" y="0" width="14" height="220" fill="url(#hero-scan)" />
        <line x1="-7" y1="18" x2="-7" y2="210" stroke="var(--ip-primary)" stroke-width="1.6" opacity="0.85" />
      </g>

      <!-- 底部渐隐（与面板底色融合） -->
      <rect x="0" y="204" width="400" height="16" fill="var(--ip-panel)" opacity="0.9" />
    </svg>
  </div>
</template>

<style scoped lang="scss">
.hero-art {
  position: relative;
  height: 176px;
  border: 1px solid var(--ip-line);
  border-radius: var(--ip-radius);
  background:
    radial-gradient(120% 90% at 85% 8%, var(--ip-primary-weak) 0%, transparent 55%),
    var(--ip-panel-2);
  overflow: hidden;

  &__svg {
    width: 100%;
    height: 100%;
    display: block;
  }

  /* 扫描线：6s 一个来回，缓入缓出。reduced-motion 时由 base.scss 全局归零 */
  &__scan {
    animation: hero-scan 6s ease-in-out infinite;
  }

  /* 检出脉冲点：错峰呼吸 */
  &__pulse {
    animation: hero-pulse 2.8s var(--ip-ease) infinite;
    transform-box: fill-box;
    transform-origin: center;

    &--late {
      animation-delay: 0.7s;
    }

    &--later {
      animation-delay: 1.4s;
    }
  }

  /* 设备在线指示灯 */
  &__led {
    animation: hero-led 1.8s ease-in-out infinite;
    transform-box: fill-box;
    transform-origin: center;
  }

  &__post {
    /* 立杆整体轻微随扫描线摆动，像风吹过 —— 幅度极小 */
    animation: hero-sway 6s ease-in-out infinite;
    transform-box: fill-box;
    transform-origin: 318px 200px;
  }
}

@keyframes hero-scan {
  0%,
  100% {
    transform: translateX(30px);
    opacity: 0.25;
  }
  50% {
    transform: translateX(370px);
    opacity: 1;
  }
}

@keyframes hero-pulse {
  0%,
  100% {
    opacity: 0.35;
    transform: scale(1);
  }
  50% {
    opacity: 1;
    transform: scale(1.35);
  }
}

@keyframes hero-led {
  0%,
  100% {
    opacity: 0.35;
  }
  50% {
    opacity: 1;
  }
}

@keyframes hero-sway {
  0%,
  100% {
    transform: rotate(0deg);
  }
  50% {
    transform: rotate(0.5deg);
  }
}

/* 深浅主题下的氛围微调 */
[data-theme='light'] & {
  border-color: var(--ip-line-soft);
}
</style>
