<script setup lang="ts">
import { computed } from 'vue'
import { ElImage } from 'element-plus'
import type { SpeciesOut } from '@/types'

const props = defineProps<{
  species: SpeciesOut
}>()

const imageUrl = computed(() => {
  const path = props.species.image_path
  if (!path) return ''
  if (/^https?:\/\//i.test(path) || path.startsWith('data:') || path.startsWith('blob:')) return path
  return path.startsWith('/') ? path : `/${path}`
})

const fields = computed(() => [
  { label: '寄主作物', value: props.species.host_crops, tone: 'default' },
  { label: '危害特征', value: props.species.damage, tone: 'default' },
  { label: '防治建议', value: props.species.control, tone: 'primary' },
  { label: '防治参考阈值', value: props.species.threshold_desc, tone: 'warn' }
])
</script>

<template>
  <article class="species-card">
    <header class="species-card__head">
      <div class="species-card__title-group">
        <h3 class="species-card__zh">{{ species.zh_name }}</h3>
        <p class="species-card__en">{{ species.en_name }}</p>
      </div>
    </header>

    <div class="species-card__body">
      <div class="species-card__thumb">
        <ElImage
          v-if="imageUrl"
          :src="imageUrl"
          fit="cover"
          class="species-card__image"
          :preview-src-list="[imageUrl]"
          preview-teleported
          :alt="`${species.zh_name} 参考图`"
        >
          <template #error>
            <div class="species-card__image-fallback">暂无参考图</div>
          </template>
        </ElImage>
        <div v-else class="species-card__image-fallback">暂无参考图</div>
      </div>

      <dl class="species-card__fields">
        <div v-for="field in fields" :key="field.label" class="species-card__field">
          <dt>{{ field.label }}</dt>
          <dd :class="`species-card__value species-card__value--${field.tone}`">
            {{ field.value || '未提供' }}
          </dd>
        </div>
      </dl>
    </div>
  </article>
</template>

<style scoped lang="scss">
.species-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  background: var(--ip-panel);
  border: 1px solid var(--ip-line);
  border-radius: var(--ip-radius);
  box-shadow: var(--ip-inner);

  &__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--ip-line-soft);
  }

  &__zh {
    font-size: var(--ip-fs-body);
    font-weight: 600;
    color: var(--ip-text);
  }

  &__en {
    font-size: var(--ip-fs-xs);
    font-style: italic;
    color: var(--ip-text-3);
  }

  &__id {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
    flex: 0 0 auto;
  }

  &__body {
    display: flex;
    gap: 12px;
    min-height: 0;
  }

  &__thumb {
    flex: 0 0 auto;
    width: 112px;
    height: 112px;
    border: 1px solid var(--ip-line);
    border-radius: var(--ip-radius-sm);
    overflow: hidden;
    background: var(--ip-panel-2);
  }

  &__image {
    width: 100%;
    height: 100%;
    display: block;
    cursor: zoom-in;
  }

  /* 只在"本机没有数据集"时出现（否则 15 类都有实拍图）；
     做成虚线弱提示，读起来是"这里本来可以放图"，而不是一块坏掉的灰块 */
  &__image-fallback {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 100%;
    border: 1px dashed var(--ip-line);
    border-radius: var(--ip-radius-sm);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__fields {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin: 0;
    min-width: 0;
  }

  &__field {
    display: flex;
    flex-direction: column;
    gap: 1px;

    dt {
      font-size: var(--ip-fs-xs);
      letter-spacing: 0.03em;
      color: var(--ip-text-3);
    }

    dd.species-card__value {
      margin: 0;
      font-size: var(--ip-fs-xs);
      line-height: 1.55;
      color: var(--ip-text-2);
    }

    dd.species-card__value--primary {
      color: var(--ip-primary-hover);
    }

    dd.species-card__value--warn {
      color: var(--ip-warn);
    }
  }
}

@media (max-width: 639px) {
  .species-card__body {
    flex-direction: column;
  }

  .species-card__thumb {
    width: 100%;
    height: 132px;
  }
}
</style>
