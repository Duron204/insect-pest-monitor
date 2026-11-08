<script setup lang="ts">
/**
 * AI 回答内容渲染：纯结构化渲染，不用 v-html（杜绝 XSS 面）。
 * 解析逻辑在 utils/aiMarkdown.ts（有单测）；这里只做映射。
 */
import { computed } from 'vue'
import { parseBlocks, type InlineSegment } from '@/utils/aiMarkdown'

const props = defineProps<{ content: string }>()
const emit = defineEmits<{ (e: 'cite', n: number): void }>()

const blocks = computed(() => parseBlocks(props.content))
</script>

<template>
  <div class="ai-message">
    <template v-for="(block, bi) in blocks" :key="bi">
      <p v-if="block.type === 'paragraph'" class="ai-message__p">
        <template v-for="(line, li) in block.lines" :key="li">
          <template v-for="(seg, si) in line" :key="si">
            <strong v-if="seg.kind === 'bold'">{{ seg.text }}</strong>
            <button
              v-else-if="seg.kind === 'cite'"
              class="ai-message__cite"
              type="button"
              :aria-label="`引用 ${seg.n}`"
              @click="emit('cite', seg.n)"
            >
              [{{ seg.n }}]
            </button>
            <template v-else>{{ seg.text }}</template>
          </template>
          <br v-if="li < block.lines.length - 1" />
        </template>
      </p>

      <ul v-else class="ai-message__list">
        <li v-for="(line, li) in block.lines" :key="li">
          <template v-for="(seg, si) in line" :key="si">
            <strong v-if="seg.kind === 'bold'">{{ seg.text }}</strong>
            <button
              v-else-if="seg.kind === 'cite'"
              class="ai-message__cite"
              type="button"
              :aria-label="`引用 ${seg.n}`"
              @click="emit('cite', seg.n)"
            >
              [{{ seg.n }}]
            </button>
            <template v-else>{{ seg.text }}</template>
          </template>
        </li>
      </ul>
    </template>
  </div>
</template>

<style scoped lang="scss">
.ai-message {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: var(--ip-fs-sm);
  line-height: 1.65;

  &__p {
    margin: 0;
    white-space: normal;
  }

  &__list {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin: 0;
    padding: 0;
    list-style: none;

    li {
      position: relative;
      padding-left: 14px;

      &::before {
        content: '';
        position: absolute;
        left: 2px;
        top: 0.62em;
        width: 5px;
        height: 5px;
        border-radius: 50%;
        background: var(--ip-primary);
        opacity: 0.75;
      }
    }
  }

  strong {
    color: var(--ip-text);
    font-weight: 600;
  }

  &__cite {
    padding: 0 2px;
    border: none;
    background: none;
    color: var(--ip-primary-hover);
    font: inherit;
    font-weight: 600;
    cursor: pointer;
    vertical-align: baseline;

    &:hover {
      text-decoration: underline;
    }
  }
}
</style>
