/**
 * 批量模式下「整批次」与「当前帧」两种口径的回归测试。
 *
 * 起因是一个真实缺陷：画布底图只有**当前那一张**（`previewUrl` → `activeBatchItem`），
 * 而画布拿到的行却是**整个批次**的累加（`activeRows` → `batchRows`），于是 57 张图的
 * 检测框全糊到当前这一张上 —— 表现为满屏乱框，且框的位置和画面里的目标对不上。
 *
 * 这里钉住三件事：
 *   1. `activeFrameRows` 只含当前这一张的行；
 *   2. 它的 key 必须**来自 `batchRows`**（不能重建），否则表格行与画布框的联动会断；
 *   3. 点到别的图的行时自动切到那一张。
 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { toDetectionRows, useSessionStore } from '@/stores/session'
import type { BatchDetectionOut, BatchItemOut, Detection } from '@/types'

function detection(score: number): Detection {
  return {
    class_id: 1,
    class_name: 'uroleucon_compositae',
    zh_name: '合头菊蚜',
    score,
    bbox: [10, 20, 30, 40],
    track_id: null
  }
}

function item(filename: string, scores: number[]): BatchItemOut {
  return {
    filename,
    image_url: `/media/outputs/${filename}`,
    width: 416,
    height: 416,
    detections: scores.map(detection),
    counts: { 合头菊蚜: scores.length },
    frame_total: scores.length,
    inference_ms: 263,
    error: null
  }
}

function batchOf(items: BatchItemOut[]): BatchDetectionOut {
  return {
    task_id: 1,
    total: items.length,
    succeeded: items.length,
    failed: 0,
    total_objects: items.reduce((sum, it) => sum + it.frame_total, 0),
    counts: {},
    items,
    elapsed_ms: 1000
  }
}

/**
 * 走真实的数据装配路径：`batchRows` 与 store 的 `buildBatchRows()` 一样，
 * 用同一个 `toDetectionRows` 以 `batch{i}` 为前缀铺出来。
 */
function loadBatch(session: ReturnType<typeof useSessionStore>, items: BatchItemOut[]): void {
  session.mode = 'batch'
  session.batchResult = batchOf(items)
  session.batchRows = items.flatMap((it, index) =>
    toDetectionRows(it.detections, it.filename, index, `batch${index}`)
  )
  session.batchIndex = 0
}

describe('批量模式下的「当前帧」口径', () => {
  let session: ReturnType<typeof useSessionStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    session = useSessionStore()
  })

  it('activeRows 是整批次（表格用），activeFrameRows 只有当前这一张（画布用）', () => {
    loadBatch(session, [
      item('C00001.jpg', [0.91]),
      item('C00009.jpg', [0.88, 0.72]),
      item('C00050.jpg', [0.95])
    ])

    // 表格：批次明细日志，三张图的行都要在
    expect(session.activeRows).toHaveLength(4)
    expect(new Set(session.activeRows.map((row) => row.frameLabel))).toEqual(
      new Set(['C00001.jpg', 'C00009.jpg', 'C00050.jpg'])
    )

    // 画布：底图只有当前这一张，行也必须只有这一张的
    expect(session.activeFrameRows).toHaveLength(1)
    expect(session.activeFrameRows.every((row) => row.frameLabel === 'C00001.jpg')).toBe(true)
  })

  it('切到下一张时 activeFrameRows 跟着走', () => {
    loadBatch(session, [
      item('C00001.jpg', [0.91]),
      item('C00009.jpg', [0.88, 0.72])
    ])

    session.selectBatchIndex(1)
    expect(session.activeFrameRows).toHaveLength(2)
    expect(session.activeFrameRows.every((row) => row.frameLabel === 'C00009.jpg')).toBe(true)
    // 批次的仍然不动
    expect(session.activeRows).toHaveLength(3)
  })

  it('activeFrameRows 的 key 必须来自 batchRows —— 重建会让联动断掉', () => {
    const items = [item('C00001.jpg', [0.91, 0.8]), item('C00009.jpg', [0.88])]
    loadBatch(session, items)

    const batchKeys = new Set(session.batchRows.map((row) => row.key))
    for (const row of session.activeFrameRows) {
      expect(batchKeys.has(row.key)).toBe(true)
    }
    // 反证：如果用 toDetectionRows 重建一遍（key 每次调用都是新生成的），
    // 得到的 key 集合与 batchRows 完全不同 —— 联动正是靠这份 key 对齐的
    const rebuilt = toDetectionRows(items[0]!.detections, 'C00001.jpg', 0, 'batch0')
    expect(rebuilt.map((row) => row.key)).not.toEqual(session.activeFrameRows.map((row) => row.key))
  })

  it('点表格里别的图的行时自动切到那一张，框才看得见', () => {
    loadBatch(session, [item('C00001.jpg', [0.91]), item('C00009.jpg', [0.88])])

    const otherFrameRow = session.batchRows.find((row) => row.frameLabel === 'C00009.jpg')!
    session.selectRow(otherFrameRow.key)

    expect(session.batchIndex).toBe(1)
    expect(session.selectedRowKey).toBe(otherFrameRow.key) // 切图不能把选中清掉
    expect(session.activeFrameRows.map((row) => row.key)).toContain(otherFrameRow.key)
  })

  it('同帧内点选不应该切图', () => {
    loadBatch(session, [item('C00001.jpg', [0.91]), item('C00009.jpg', [0.88])])

    const sameFrameRow = session.batchRows.find((row) => row.frameLabel === 'C00001.jpg')!
    session.selectRow(sameFrameRow.key)

    expect(session.batchIndex).toBe(0)
    expect(session.selectedRowKey).toBe(sameFrameRow.key)
  })

  it('非批量模式下两者必须完全一致，不能顺手改坏单图检测', () => {
    session.mode = 'image'
    session.imageRows = toDetectionRows([detection(0.7), detection(0.9)], 'a.jpg', 0, 'img')

    expect(session.activeFrameRows).toEqual(session.activeRows)
    expect(session.activeFrameRows).toHaveLength(2)
  })
})
