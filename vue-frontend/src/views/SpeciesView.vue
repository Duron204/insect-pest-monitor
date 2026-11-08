<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElButton, ElInput } from 'element-plus'
import SpeciesCard from '@/components/species/SpeciesCard.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorBanner from '@/components/common/ErrorBanner.vue'
import { listSpecies, speciesDisclaimer } from '@/api/species'
import { readString, writeString } from '@/utils/storage'
import { notifyApiError } from '@/utils/notify'
import type { SpeciesOut } from '@/types'

/** 声明是否展开：记住选择；默认展开（这是需要先读的提示，不该默认藏起来） */
const DISCLAIMER_KEY = 'ip_species_disclaimer_open'

const species = ref<SpeciesOut[]>([])
const disclaimer = ref<string[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const keyword = ref('')
const showDisclaimer = ref(readString(DISCLAIMER_KEY, '1') === '1')

function toggleDisclaimer(): void {
  showDisclaimer.value = !showDisclaimer.value
  writeString(DISCLAIMER_KEY, showDisclaimer.value ? '1' : '0')
}

const filtered = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  if (text.length === 0) return species.value
  return species.value.filter((item) => {
    return (
      item.zh_name.toLowerCase().includes(text) ||
      item.en_name.toLowerCase().includes(text) ||
      (item.host_crops ?? '').toLowerCase().includes(text)
    )
  })
})

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const [list, note] = await Promise.all([
      listSpecies(),
      speciesDisclaimer().catch(() => ({ disclaimer: [] as string[] }))
    ])
    species.value = list ?? []
    disclaimer.value = note.disclaimer ?? []
  } catch (err) {
    error.value = notifyApiError(err)
    species.value = []
  } finally {
    loading.value = false
  }
}

onMounted(load)

function onEmptyAction(): void {
  if (species.value.length === 0) {
    void load()
    return
  }
  keyword.value = ''
}
</script>

<template>
  <div class="species-view">
    <!-- 声明：可收起并记住选择。收起后**保留一条细提示** ——
         原实现收起后整块消失、再也打不开，等于把这份提示弄丢了 -->
    <section
      v-if="disclaimer.length > 0"
      class="species-view__disclaimer"
      :class="{ 'is-collapsed': !showDisclaimer }"
      role="note"
    >
      <header class="species-view__disclaimer-head">
        <h2 class="species-view__disclaimer-title">重要声明</h2>
        <ElButton
          text
          size="small"
          :aria-label="showDisclaimer ? '收起声明' : '展开声明'"
          @click="toggleDisclaimer"
        >
          {{ showDisclaimer ? '收起' : '展开' }}
        </ElButton>
      </header>

      <template v-if="showDisclaimer">
        <ul class="species-view__disclaimer-list">
          <li v-for="(line, index) in disclaimer" :key="index">{{ line }}</li>
        </ul>
        <p class="species-view__disclaimer-foot">
          以下防治建议与阈值为参考资料，实际决策请结合当地植保部门指导与田间实况，必要时由专家复核。
        </p>
      </template>
      <p v-else class="species-view__disclaimer-peek">
        防治建议与阈值仅供参考，实际决策请结合当地植保部门指导与田间实况。
      </p>
    </section>

    <header class="species-view__bar ip-panel">
      <div class="species-view__bar-left">
        <h2 class="ip-section-title">虫害知识库</h2>
        <span class="species-view__count num">
          {{ filtered.length }} / {{ species.length }} 条
        </span>
      </div>
      <div class="species-view__bar-right">
        <ElInput
          v-model="keyword"
          size="small"
          clearable
          placeholder="搜索中文名 / 学名 / 寄主作物"
          aria-label="搜索虫害条目"
          class="species-view__search"
        />
        <ElButton size="small" :loading="loading" aria-label="刷新知识库" @click="load">
          刷新
        </ElButton>
      </div>
    </header>

    <ErrorBanner :message="error" title="知识库加载失败" @close="error = null" />

    <EmptyState
      v-if="filtered.length === 0 && !loading && !error"
      :description="species.length === 0 ? '知识库暂时没有数据' : '没有匹配的虫害条目'"
      :hint="
        species.length === 0
          ? '后端 /api/species 应返回 15 条记录，请确认后端服务与数据文件完整'
          : '换个关键词试试，例如「稻飞虱」或「玉米」'
      "
      :action-text="species.length === 0 ? '重新加载' : '清空搜索'"
      @action="onEmptyAction"
    />

    <section v-else class="species-view__grid">
      <SpeciesCard v-for="item in filtered" :key="item.class_id" :species="item" />
    </section>
  </div>
</template>

<style scoped lang="scss">
.species-view {
  display: flex;
  flex-direction: column;
  gap: var(--ip-gap);
  padding: var(--ip-gap);
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;

  &__disclaimer {
    padding: 12px 14px;
    background: var(--ip-warn-weak);
    border: 1px solid var(--ip-warn-line);
    border-radius: var(--ip-radius);
  }

  &__disclaimer-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  &__disclaimer-title {
    font-size: var(--ip-fs-sm);
    font-weight: 600;
    color: var(--ip-warn);
  }

  &__disclaimer-list {
    display: flex;
    flex-direction: column;
    gap: 3px;
    margin-top: 6px;

    li {
      font-size: var(--ip-fs-xs);
      line-height: 1.6;
      color: var(--ip-text-2);
    }

    li::before {
      content: '· ';
      color: var(--ip-warn);
    }
  }

  &__disclaimer-foot {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px dashed var(--ip-warn-line);
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    padding: 8px 12px;
  }

  &__bar-left {
    display: flex;
    align-items: baseline;
    gap: 10px;
  }

  &__count {
    font-size: var(--ip-fs-xs);
    color: var(--ip-text-3);
  }

  &__bar-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  &__search {
    width: 240px;
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(392px, 1fr));
    gap: var(--ip-gap);
  }
}

@media (max-width: 1023px) {
  .species-view__grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .species-view__search {
    width: 100%;
  }
}
/** 收起状态下保留的一行提示（不能因为收起就把这份声明弄丢） */
.species-view__disclaimer-peek {
  margin: 2px 0 0;
  font-size: var(--ip-fs-xs);
  line-height: 1.6;
  color: var(--ip-text-3);
}
</style>
