<template>
  <div class="pipeline-container space-y-5">
    <!-- 主卡片：文档处理进度轴 -->
    <div class="bg-white border border-slate-200/90 rounded-xl p-5 sm:p-6 shadow-sm">
      <!-- 顶栏状态与说明 -->
      <div class="flex flex-wrap items-center justify-between gap-3 pb-4 mb-4 border-b border-slate-100">
        <div class="flex items-center space-x-3">
          <div class="w-3 h-3 rounded-full" :class="overallStatusDotClass"></div>
          <div>
            <div class="flex items-center gap-2">
              <h3 class="text-sm font-bold text-slate-900 tracking-tight">文档处理进度</h3>
              <span class="text-xs px-2.5 py-0.5 rounded-full font-medium border" :class="statusBadgeClass">
                {{ currentStatusText }}
              </span>
            </div>
            <div v-if="docId" class="text-[11px] text-slate-400 font-mono mt-0.5 flex items-center gap-2">
              <span>文档 ID: <span class="text-slate-700 font-medium">{{ docId }}</span></span>
              <span v-if="fileName" class="text-slate-500 font-sans">· {{ fileName }}</span>
            </div>
          </div>
        </div>

        <div class="flex items-center space-x-2">
          <!-- 若处于防重预警挂起状态，在顶栏放置醒目的处理决策按钮 -->
          <button 
            v-if="overallStatus === 'duplicate_warning'"
            @click="$emit('duplicateWarning', matchedDocId)"
            class="text-xs text-white bg-orange-600 hover:bg-orange-700 border border-orange-700 px-3 py-1.5 rounded-lg flex items-center gap-1.5 font-medium shadow-sm transition-all cursor-pointer animate-pulse"
          >
            <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
            <span>处理相似文档</span>
          </button>

          <!-- 刷新状态 -->
          <button 
            @click="reconnect" 
            :disabled="isReconnecting"
            class="text-xs text-blue-700 hover:text-blue-800 bg-blue-50 hover:bg-blue-100/80 border border-blue-200 px-3 py-1.5 rounded-lg flex items-center gap-1.5 font-medium transition-all disabled:opacity-50 cursor-pointer"
          >
            <svg class="w-3.5 h-3.5" :class="{ 'animate-spin': isReconnecting }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/>
            </svg>
            <span>刷新状态</span>
          </button>
        </div>
      </div>

      <!-- 11 节点全量流转轴 -->
      <div class="relative py-4 overflow-x-auto min-w-[880px]">
        <!-- 导轨连线 (贯穿 11 个节点中心) -->
        <div class="absolute left-10 right-10 top-8 h-0.5 bg-slate-200 -z-0"></div>

        <div class="flex items-start justify-between relative z-10 w-full px-2">
          <div 
            v-for="step in steps" 
            :key="step.index"
            class="flex flex-col items-center text-center group cursor-default w-20 shrink-0"
          >
            <!-- 节点圆形徽标 -->
            <div 
              class="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold transition-all duration-300 relative bg-white shadow-sm"
              :class="getNodeClass(step)"
            >
              <!-- 完成态：对勾 -->
              <svg v-if="step.status === 'finish'" class="w-4 h-4 text-emerald-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
                <polyline points="20 6 9 17 4 12"/>
              </svg>
              <!-- 跳过态：横杠 -->
              <svg v-else-if="step.status === 'skipped'" class="w-3.5 h-3.5 text-slate-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                <line x1="6" y1="12" x2="18" y2="12"/>
              </svg>
              <!-- 进行中：呼吸光晕 -->
              <span v-else-if="step.status === 'process'" class="relative flex items-center justify-center">
                <span class="animate-ping absolute w-4 h-4 rounded-full bg-blue-400 opacity-60"></span>
                <span class="w-2.5 h-2.5 rounded-full bg-blue-600"></span>
              </span>
              <!-- 预警态：呼吸橙色标与感叹号 -->
              <span v-else-if="step.status === 'warning'" class="relative flex items-center justify-center">
                <span class="animate-ping absolute w-4 h-4 rounded-full bg-orange-400 opacity-60"></span>
                <svg class="w-4 h-4 text-orange-600 relative z-10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
              </span>
              <!-- 失败态：感叹号 -->
              <svg v-else-if="step.status === 'error'" class="w-4 h-4 text-rose-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
                <line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
              </svg>
              <!-- 等待态：序号数字 -->
              <span v-else class="text-[11px] text-slate-400 font-mono">{{ step.index }}</span>
            </div>

            <!-- 节点名称与状态 -->
            <div class="mt-2.5 w-full">
              <div 
                class="text-xs font-semibold tracking-tight whitespace-nowrap transition-colors"
                :class="[
                  step.status === 'process' ? 'text-blue-600' :
                  step.status === 'finish' ? 'text-emerald-700' :
                  step.status === 'skipped' ? 'text-slate-400' :
                  step.status === 'warning' ? 'text-orange-600 font-bold' :
                  step.status === 'error' ? 'text-rose-600 font-bold' : 'text-slate-600'
                ]"
              >
                {{ step.name }}
              </div>
              <div 
                class="text-[10px] mt-0.5 font-mono truncate px-1"
                :class="step.status === 'finish' ? 'text-emerald-600 font-medium' : step.status === 'warning' ? 'text-orange-600 font-bold' : 'text-slate-400'"
                :title="step.detail"
              >
                {{
                  step.status === 'finish' ? '完成' :
                  step.status === 'skipped' ? (step.detail && step.detail.includes('无需审核') ? '无需审核' : '已跳过') :
                  step.status === 'process' ? '执行中' :
                  step.status === 'warning' ? '待确认' :
                  step.status === 'error' ? '失败' : '等待'
                }}
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 错误详情呈现面板 (支持复制与断点重试续跑) -->
      <div 
        v-if="overallStatus === 'failed' && (errorMessage || currentFailedDetail)"
        class="mt-4 p-4 rounded-xl bg-rose-50/90 border border-rose-200 text-xs text-rose-900 shadow-sm animate-in fade-in duration-200"
      >
        <div class="flex items-start gap-3">
          <div class="w-7 h-7 rounded-lg bg-rose-100 flex items-center justify-center shrink-0 text-rose-600 mt-0.5">
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
          </div>
          <div class="flex-1 min-w-0">
            <div class="flex items-center justify-between gap-2">
              <span class="font-bold text-rose-800 text-xs sm:text-sm">
                第 {{ currentStepIndex }} 步 [{{ currentStepName }}] 执行异常
              </span>
              <div class="flex items-center gap-2 shrink-0">
                <button 
                  type="button"
                  @click="copyError" 
                  class="px-2.5 py-1 rounded-md bg-rose-100 hover:bg-rose-200 text-rose-700 text-[11px] font-medium transition-colors cursor-pointer flex items-center gap-1"
                >
                  <svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                  </svg>
                  <span>{{ copied ? '已复制' : '复制错误' }}</span>
                </button>
                <!-- 一键重试按钮 (断点续跑) -->
                <button 
                  type="button"
                  :disabled="isRetrying"
                  @click="handleRetryCurrentStep" 
                  class="px-3 py-1 rounded-md bg-rose-600 hover:bg-rose-700 text-white text-[11px] font-medium transition-colors shadow-sm flex items-center gap-1.5 cursor-pointer disabled:opacity-50"
                >
                  <svg v-if="isRetrying" class="animate-spin w-3 h-3" viewBox="0 0 24 24" fill="none">
                    <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" class="opacity-25"></circle>
                    <path fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" class="opacity-75"></path>
                  </svg>
                  <svg v-else class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16"/>
                  </svg>
                  <span>{{ isRetrying ? '正在重试...' : '重试当前步骤' }}</span>
                </button>
              </div>
            </div>
            <p class="mt-2 font-mono text-[11px] leading-relaxed text-rose-700 bg-white/80 p-3 rounded-lg border border-rose-200/80 break-all whitespace-pre-wrap select-text">
              {{ errorMessage || currentFailedDetail }}
            </p>
          </div>
        </div>
      </div>
    </div>

    <!-- 底部双栏监控面板 -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-5 items-start">
      <!-- 左侧：图表与代码明细 (占 7 列) -->
      <div class="lg:col-span-7">
        <div class="bg-white border border-slate-200/90 rounded-xl p-5 shadow-sm flex flex-col h-[540px]">
          <!-- 顶栏：标题与操作 -->
          <div class="flex items-center justify-between pb-3 border-b border-slate-100 shrink-0">
            <div>
              <h4 class="text-xs font-bold text-slate-900 tracking-tight">图表与代码明细</h4>
            </div>

            <!-- 若处于第 7 步审核挂起状态，显示醒目操作按钮 -->
            <button 
              v-if="overallStatus === 'pending_review'"
              @click="$emit('reviewRequired', pendingReviews)"
              class="px-3 py-1.5 bg-amber-500 hover:bg-amber-600 text-white rounded-lg text-xs font-medium shadow-sm flex items-center gap-1.5 transition-colors cursor-pointer animate-bounce"
            >
              <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
              <span>打开人工审核 ({{ pendingReviews.length }})</span>
            </button>
            <!-- 若处于第 11 步防重预警挂起状态，显示醒目操作按钮 -->
            <button 
              v-else-if="overallStatus === 'duplicate_warning'"
              @click="$emit('duplicateWarning', matchedDocId)"
              class="px-3 py-1.5 bg-orange-600 hover:bg-orange-700 text-white rounded-lg text-xs font-medium shadow-sm flex items-center gap-1.5 transition-colors cursor-pointer animate-pulse"
            >
              <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
              <span>处理相似文档确认</span>
            </button>
            <div v-else-if="pendingReviews.length > 0" class="text-[11px] text-slate-500 font-mono">
              共提取 <span class="font-bold text-slate-800">{{ pendingReviews.length }}</span> 项
            </div>
          </div>

          <!-- 多模态图元交互分类筛选分段器 (Segmented Tabs，一体化去框降噪，兼具分类与统计) -->
          <div class="flex items-center gap-1.5 p-1 bg-slate-100/90 rounded-xl my-3 shrink-0 text-xs overflow-x-auto">
            <button
              v-for="tab in multimodalTabs"
              :key="tab.key"
              @click="selectedMultimodalTab = tab.key"
              class="px-3 py-1.5 rounded-lg font-medium transition-all flex items-center gap-1.5 whitespace-nowrap cursor-pointer text-xs"
              :class="selectedMultimodalTab === tab.key 
                ? 'bg-white text-slate-900 shadow-sm font-semibold' 
                : 'text-slate-500 hover:text-slate-800 hover:bg-slate-200/50'"
            >
              <span>{{ tab.label }}</span>
              <span 
                class="px-1.5 py-0.5 rounded-full text-[10px] font-mono"
                :class="selectedMultimodalTab === tab.key ? 'bg-blue-50 text-blue-700 font-semibold' : 'bg-slate-200/60 text-slate-500'"
              >
                {{ tab.count }}
              </span>
            </button>
          </div>

          <!-- 待审核预警横幅 (若挂起) -->
          <div 
            v-if="overallStatus === 'pending_review'" 
            class="mb-3 p-3 rounded-lg bg-amber-50/90 border border-amber-200 text-xs text-amber-900 flex items-start gap-2.5 shrink-0"
          >
            <div class="w-4 h-4 rounded-md bg-amber-200 text-amber-800 flex items-center justify-center shrink-0 mt-0.5 text-[11px] font-bold">
              !
            </div>
            <div class="flex-1 text-[11px] leading-relaxed">
              <span class="font-bold text-amber-900">等待人工审核确认。</span>
              请点击右上角按钮核对提取的图表与代码说明，确认后继续完成入库。
            </div>
          </div>

          <!-- 防重预警决策横幅 (若挂起) -->
          <div 
            v-if="overallStatus === 'duplicate_warning'" 
            class="mb-3 p-3.5 rounded-lg bg-orange-50/95 border border-orange-200 text-xs text-orange-950 flex items-center justify-between gap-3 shrink-0"
          >
            <div class="flex items-start gap-2.5">
              <div class="w-4 h-4 rounded-md bg-orange-200 text-orange-800 flex items-center justify-center shrink-0 mt-0.5 text-[11px] font-bold">
                !
              </div>
              <div class="flex-1 text-[11px] leading-relaxed">
                <span class="font-bold text-orange-900">检测到知识库中已存在相似文档，入库已暂停。</span>
                请选择覆盖更新原文档或作为新文档独立入库。
              </div>
            </div>
            <button 
              @click="$emit('duplicateWarning', matchedDocId)"
              class="shrink-0 px-3.5 py-1.5 bg-orange-600 hover:bg-orange-700 text-white rounded-lg text-xs font-medium shadow-sm transition-colors cursor-pointer"
            >
              立即处理 &rarr;
            </button>
          </div>

          <!-- 可滚动内容区：真实图元列表 或 真实状态反馈 -->
          <div class="flex-1 overflow-y-auto pr-1 space-y-2.5">
            <!-- 场景 1：真实提取出多模态图元时的扁平化条目列表 -->
            <div v-if="filteredPendingReviews.length > 0" class="space-y-2.5">
              <div 
                v-for="(item, idx) in filteredPendingReviews" 
                :key="item.item_id"
                class="p-3 rounded-xl border border-slate-200/90 bg-white hover:border-slate-300 transition-all space-y-2.5 shadow-sm group"
              >
                <div class="flex items-center justify-between text-xs gap-2">
                  <div class="flex items-center gap-2 min-w-0">
                    <span 
                      class="px-1.5 py-0.5 rounded text-[10px] font-mono font-medium border shrink-0"
                      :class="getTypeBadgeClass(item.type)"
                    >
                      {{ getTypeLabel(item.type) }}
                    </span>
                    <span class="font-bold text-slate-800 truncate text-xs">{{ item.title || `条目 ${idx + 1}` }}</span>
                  </div>
                  <div class="flex items-center gap-2 text-[11px] text-slate-400 font-mono shrink-0">
                    <span>第 {{ item.display_page || (item.page_idx + 1) }} 页</span>
                    <span 
                      class="px-1.5 py-0.5 rounded text-[10px]"
                      :class="(item.status === 'approved' || item.status === 'modified') ? 'bg-emerald-50 text-emerald-600 border border-emerald-200' : 'bg-slate-100 text-slate-600'"
                    >
                      {{ item.status === 'modified' ? '已修订' : item.status === 'approved' ? '已核对' : (overallStatus === 'pending_review' ? '待核对' : '已提取') }}
                    </span>
                    <!-- 快捷核对按钮 -->
                    <button
                      v-if="overallStatus === 'pending_review'"
                      @click.stop="$emit('reviewRequired', pendingReviews, getReviewItemIndex(item.item_id))"
                      class="text-[10px] font-sans text-amber-700 hover:text-amber-800 bg-amber-50 hover:bg-amber-100 px-2 py-0.5 rounded border border-amber-200 transition-colors cursor-pointer"
                    >
                      核对此项 &rarr;
                    </button>
                  </div>
                </div>

                <!-- 真实图元缩略图（若存在资源） -->
                <div v-if="item.asset_url || item.raw_oss_url" class="flex items-start gap-3 bg-slate-50/70 p-2.5 rounded-lg border border-slate-100">
                  <img 
                    :src="withTokenUrl(item.asset_url || item.raw_oss_url)" 
                    :alt="item.title"
                    class="h-16 w-auto max-w-[120px] object-contain rounded border border-slate-200/80 bg-white shrink-0 cursor-pointer"
                    loading="lazy"
                    @click="$emit('reviewRequired', pendingReviews, getReviewItemIndex(item.item_id))"
                    title="点击查看详情"
                  />
                  <div class="flex-1 min-w-0">
                    <div 
                      :class="isItemExpanded(item.item_id) ? 'max-h-56 overflow-y-auto pr-1' : 'line-clamp-2'"
                      class="text-xs text-slate-700 leading-relaxed break-words select-text markdown-body"
                      v-html="renderItemMarkdown(item.user_description || item.vlm_description || '暂无说明')"
                    ></div>
                    <div 
                      v-if="(item.vlm_description || item.user_description || '').length > 70"
                      class="flex items-center justify-end pt-1"
                    >
                      <button 
                        @click.stop="toggleExpandItem(item.item_id)"
                        class="text-[10px] text-blue-600 hover:text-blue-700 font-sans font-medium flex items-center gap-0.5 cursor-pointer"
                      >
                        <span>{{ isItemExpanded(item.item_id) ? '收起 ▲' : '展开全文 ▼' }}</span>
                      </button>
                    </div>
                  </div>
                </div>

                <!-- 纯文本描述（如表格语义、流程图文本或代码块摘要） -->
                <div v-else class="bg-slate-50/70 p-2.5 rounded-lg border border-slate-100">
                  <div 
                    :class="isItemExpanded(item.item_id) ? 'max-h-56 overflow-y-auto pr-1' : 'line-clamp-3'"
                    class="text-xs text-slate-700 leading-relaxed break-words select-text markdown-body"
                    v-html="renderItemMarkdown(item.user_description || item.vlm_description || item.raw_content || '暂无说明')"
                  ></div>
                  <div 
                    v-if="(item.vlm_description || item.user_description || item.raw_content || '').length > 90"
                    class="flex items-center justify-end pt-1.5 border-t border-slate-200/60 mt-1.5"
                  >
                    <button 
                      @click.stop="toggleExpandItem(item.item_id)"
                      class="text-[10px] text-blue-600 hover:text-blue-700 font-sans font-medium flex items-center gap-0.5 cursor-pointer"
                    >
                      <span>{{ isItemExpanded(item.item_id) ? '收起 ▲' : '展开全文 ▼' }}</span>
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <!-- 当前分类无项提示 -->
            <div v-else-if="pendingReviews.length > 0" class="py-16 text-center text-xs text-slate-400">
              当前分类下暂无内容
            </div>

            <!-- 场景 2：文档尚未汇总图元或纯文本时的真实状态反馈（杜绝武断提前判定） -->
            <div v-else class="h-full flex flex-col items-center justify-center text-center p-6 space-y-2.5 text-slate-400">
              <!-- 2.1 任务执行异常中断报错 -->
              <template v-if="overallStatus === 'failed'">
                <div class="w-10 h-10 rounded-full bg-rose-50 text-rose-600 flex items-center justify-center border border-rose-200">
                  <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
                </div>
                <div class="text-xs font-bold text-rose-700">文档处理中断</div>
                <p class="text-[11px] text-rose-600/90 max-w-[320px] font-mono break-all line-clamp-3">
                  {{ errorMessage || currentFailedDetail || '当前步骤执行异常，处理流程已停止。' }}
                </p>
              </template>

              <!-- 2.2 正在处于第 1~7 节点多模态处理与结构化阶段 (绝对不能判为未检出！) -->
              <template v-else-if="isMultimodalPhase">
                <div class="w-9 h-9 rounded-full border-2 border-blue-600 border-t-transparent animate-spin mb-1"></div>
                <div class="text-xs font-bold text-slate-800 flex items-center gap-1.5">
                  <span class="inline-block w-2 h-2 rounded-full bg-blue-600 animate-ping"></span>
                  <span>正在解析文档版面与图表...</span>
                </div>
                <p class="text-[11px] text-slate-500 max-w-[320px] leading-relaxed">
                  {{ multimodalProcessingText }}
                </p>
                <div class="text-[10px] text-slate-400 font-mono mt-1">
                  提取完成后将在此展示表格、图表与代码明细
                </div>
              </template>

              <!-- 2.3 任务全量入库完成，且确属纯文本文档 (0 图元) -->
              <template v-else-if="overallStatus === 'completed'">
                <div class="w-10 h-10 rounded-full bg-slate-100 text-slate-500 flex items-center justify-center">
                  <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                </div>
                <div class="text-xs font-bold text-slate-700">纯文本文档 · 无图表或代码</div>
                <p class="text-[11px] text-slate-400 max-w-[280px]">
                  该文档已完成入库，未包含独立表格或配图。
                </p>
              </template>

              <!-- 2.4 已完成第 7 步并进入第 8~11 步，确属无图元流转 -->
              <template v-else-if="currentStepIndex >= 8">
                <div class="w-10 h-10 rounded-full bg-slate-100 text-slate-400 flex items-center justify-center">
                  <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
                </div>
                <div class="text-xs font-bold text-slate-700">纯文本文档处理中</div>
                <p class="text-[11px] text-slate-400 max-w-[280px]">
                  未包含独立表格或配图，正在执行文本切分与索引构建。
                </p>
              </template>

              <!-- 2.5 其它就绪/等待态 -->
              <template v-else>
                <div class="w-8 h-8 rounded-full bg-slate-100 text-slate-400 flex items-center justify-center">
                  <span class="text-xs">...</span>
                </div>
                <div class="text-xs font-bold text-slate-600">排队等待处理</div>
                <p class="text-[11px] text-slate-400 max-w-[260px]">
                  任务开始后将依次解析文档版面并提取内容。
                </p>
              </template>
            </div>
          </div>
        </div>
      </div>

      <!-- 右侧：实时流转控制台 (Live Pipeline Console，占 5 列) -->
      <div class="lg:col-span-5 space-y-4">
        <div class="bg-white border border-slate-200/90 rounded-xl p-5 shadow-sm flex flex-col h-[540px]">
          <!-- 控制台顶栏 -->
          <div class="flex items-center justify-between pb-3 border-b border-slate-100 shrink-0">
            <div class="flex items-center gap-2">
              <h4 class="text-xs font-bold text-slate-900 tracking-tight">处理日志</h4>
            </div>

            <div class="flex items-center space-x-2 text-[11px]">
              <button 
                @click="clearLogs" 
                class="text-slate-400 hover:text-slate-700 px-1.5 py-0.5 rounded hover:bg-slate-100 transition-colors cursor-pointer"
              >
                清空
              </button>
            </div>
          </div>

          <!-- 日志流列表 (极客流式控制台风格) -->
          <div 
            ref="logsContainerRef"
            class="flex-1 overflow-y-auto space-y-2 pt-3 font-mono text-[11px] pr-1 select-text"
          >
            <div 
              v-for="log in consoleLogs" 
              :key="log.id"
              class="flex items-start gap-2 leading-relaxed"
            >
              <span class="text-slate-400 shrink-0 text-[10px]">{{ log.time }}</span>
              <span 
                class="px-1.5 py-0.5 rounded text-[10px] shrink-0 font-medium"
                :class="log.tagClass"
              >
                {{ log.tag }}
              </span>
              <span class="text-slate-700 break-all">{{ log.message }}</span>
            </div>

            <div v-if="consoleLogs.length === 0" class="py-16 text-center text-xs text-slate-400">
              暂无处理日志
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onUnmounted, nextTick } from 'vue';
import MarkdownIt from 'markdown-it';
import type { PipelineStep, PipelineStepStatus, ReviewItem } from '../../types';
import { DEFAULT_PIPELINE_STEPS } from '../../types';
import { documentsApi } from '../../api/documents';
import { withTokenUrl } from '../../stores/auth';

const props = defineProps<{
  docId: string;
  fileName?: string;
  autoConnect?: boolean;
}>();

const emit = defineEmits<{
  (e: 'reviewRequired', items: ReviewItem[], initialIndex?: number): void;
  (e: 'duplicateWarning', matchedDocId?: string): void;
  (e: 'completed', docId: string): void;
  (e: 'failed', error: string): void;
  (e: 'statusChange', status: string): void;
  (e: 'fileNameChange', fileName: string): void;
}>();

const steps = ref<PipelineStep[]>(JSON.parse(JSON.stringify(DEFAULT_PIPELINE_STEPS)));
const overallStatus = ref<string>('wait');

watch(overallStatus, (newVal) => {
  if (newVal) {
    emit('statusChange', newVal);
  }
}, { immediate: true });
const currentStepIndex = ref<number>(1);
const isReconnecting = ref<boolean>(false);
const errorMessage = ref<string>('');
const copied = ref<boolean>(false);
const pendingReviews = ref<ReviewItem[]>([]);
const matchedDocId = ref<string | undefined>(undefined);
let eventSource: EventSource | null = null;

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
});

const renderItemMarkdown = (text?: string): string => {
  if (!text) return '';
  return md.render(text);
};

// 多模态版面图元分类 Tab 与展开收起状态
const selectedMultimodalTab = ref<string>('all');
const expandedItemIds = ref<Set<string>>(new Set());

const toggleExpandItem = (itemId: string) => {
  const next = new Set(expandedItemIds.value);
  if (next.has(itemId)) {
    next.delete(itemId);
  } else {
    next.add(itemId);
  }
  expandedItemIds.value = next;
};

const isItemExpanded = (itemId: string) => expandedItemIds.value.has(itemId);

const getReviewItemIndex = (itemId: string) => {
  const idx = pendingReviews.value.findIndex((r) => r.item_id === itemId);
  return idx >= 0 ? idx : 0;
};

// 真实多模态图元统计（严格基于当前真实数据）
const tableCount = computed(() => pendingReviews.value.filter((r) => r.type === 'table').length);
const imageCount = computed(() => pendingReviews.value.filter((r) => r.type === 'image').length);
const flowchartCount = computed(() => pendingReviews.value.filter((r) => r.type === 'flowchart').length);
const codeCount = computed(() => pendingReviews.value.filter((r) => r.type === 'code').length);

// 标签页元数据定义
const multimodalTabs = computed(() => [
  { key: 'all', label: '全部', count: pendingReviews.value.length },
  { key: 'table', label: '表格', count: tableCount.value },
  { key: 'flowchart', label: '流程图', count: flowchartCount.value },
  { key: 'code', label: '代码块', count: codeCount.value },
  { key: 'image', label: '配图', count: imageCount.value },
]);

// 依据选中分类筛选的图元列表
const filteredPendingReviews = computed(() => {
  if (selectedMultimodalTab.value === 'all') {
    return pendingReviews.value;
  }
  return pendingReviews.value.filter((r) => r.type === selectedMultimodalTab.value);
});

// 是否处于多模态图元抽取与结构化阶段 (Step 1~7)
const isMultimodalPhase = computed(() => {
  return overallStatus.value === 'running' && currentStepIndex.value <= 7;
});

// 动态获取当前处理中状态的客观描述文字
const multimodalProcessingText = computed(() => {
  const step = currentStepIndex.value;
  if (step === 6) {
    return '正在整理表格、代码块与流程图结构...';
  }
  if (step === 5) {
    return '正在生成图表与配图内容说明...';
  }
  if (step === 4) {
    return '正在保存文档图表与截图...';
  }
  if (step === 3) {
    return '正在提取文档正文结构与版面信息...';
  }
  if (step === 2) {
    return '正在解析文档版面与图表...';
  }
  if (step === 7) {
    return '正在整理待审核明细...';
  }
  return '正在处理文档内容...';
});

const getTypeBadgeClass = (type: string) => {
  switch (type) {
    case 'table': return 'bg-blue-50 text-blue-700 border-blue-200';
    case 'image': return 'bg-purple-50 text-purple-700 border-purple-200';
    case 'flowchart': return 'bg-emerald-50 text-emerald-700 border-emerald-200';
    case 'code': return 'bg-slate-100 text-slate-700 border-slate-200';
    default: return 'bg-slate-100 text-slate-700 border-slate-200';
  }
};

const getTypeLabel = (type: string) => {
  switch (type) {
    case 'table': return '表格';
    case 'image': return '配图';
    case 'flowchart': return '流程图';
    case 'code': return '代码块';
    default: return type || '条目';
  }
};

const logsContainerRef = ref<HTMLElement | null>(null);

// 实时日志流条目
interface ConsoleLog {
  id: string;
  time: string;
  tag: string;
  tagClass: string;
  message: string;
}
const consoleLogs = ref<ConsoleLog[]>([]);

const addLog = (tag: string, tagClass: string, message: string) => {
  const d = new Date();
  const time = `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}:${d.getSeconds().toString().padStart(2, '0')}`;
  consoleLogs.value.push({
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    time,
    tag,
    tagClass,
    message,
  });
  if (consoleLogs.value.length > 100) {
    consoleLogs.value.shift();
  }
  nextTick(() => {
    if (logsContainerRef.value) {
      logsContainerRef.value.scrollTop = logsContainerRef.value.scrollHeight;
    }
  });
};

const clearLogs = () => {
  consoleLogs.value = [];
};

// 状态规格化映射 (后端状态 -> 前端状态)
const normalizeStatus = (status: string): PipelineStepStatus => {
  switch (status) {
    case 'completed':
    case 'finish':
      return 'finish';
    case 'skipped':
      return 'skipped';
    case 'running':
    case 'process':
    case 'pending_review':
      return 'process';
    case 'duplicate_warning':
    case 'warning':
      return 'warning';
    case 'failed':
    case 'error':
      return 'error';
    default:
      return 'wait';
  }
};

const getNodeClass = (step: PipelineStep) => {
  if (step.status === 'finish') {
    return 'border-2 border-emerald-500 bg-emerald-50 text-emerald-600';
  }
  if (step.status === 'skipped') {
    return 'border-2 border-dashed border-slate-300 bg-slate-50 text-slate-400';
  }
  if (step.status === 'process') {
    return 'border-2 border-blue-600 bg-blue-50 text-blue-600 ring-4 ring-blue-100';
  }
  if (step.status === 'error') {
    return 'border-2 border-rose-600 bg-rose-50 text-rose-600 ring-4 ring-rose-100';
  }
  if (step.status === 'warning') {
    return 'border-2 border-orange-500 bg-orange-50 text-orange-600 ring-4 ring-orange-100';
  }
  return 'border-2 border-slate-200 text-slate-300';
};

const currentStepName = computed(() => {
  const current = steps.value.find((s) => s.index === currentStepIndex.value);
  return current ? current.name : '';
});

const currentFailedDetail = computed(() => {
  const current = steps.value.find((s) => s.index === currentStepIndex.value);
  return current?.detail || '';
});

const copyError = () => {
  const text = errorMessage.value || currentFailedDetail.value;
  if (text) {
    navigator.clipboard.writeText(text);
    copied.value = true;
    setTimeout(() => {
      copied.value = false;
    }, 2000);
  }
};

const isRetrying = ref<boolean>(false);

const handleRetryCurrentStep = async () => {
  if (!props.docId || isRetrying.value) return;
  try {
    isRetrying.value = true;
    errorMessage.value = '';
    const targetStep = currentStepIndex.value || 1;
    const retryRes = await documentsApi.retryPipeline(props.docId, targetStep);
    const actualFromStep = retryRes?.from_step || 1;

    currentStepIndex.value = actualFromStep;
    for (let i = actualFromStep; i <= 11; i++) {
      setStepStatus(i, 'wait', '');
    }
    overallStatus.value = retryRes?.status === 'queued' ? 'queued' : 'running';
    addLog(
      '队列',
      'bg-indigo-100 text-indigo-700',
      retryRes?.message || `已重新提交处理，将从第 ${actualFromStep} 步继续执行`
    );
    connectSSE();
  } catch (err: any) {
    console.error('重试请求失败:', err);
    errorMessage.value = err?.response?.data?.detail || err?.message || '发起重试请求失败';
    setStepStatus(currentStepIndex.value, 'error', errorMessage.value);
    overallStatus.value = 'failed';
    addLog('异常', 'bg-rose-100 text-rose-700', `重试发起失败: ${errorMessage.value}`);
  } finally {
    setTimeout(() => {
      isRetrying.value = false;
    }, 800);
  }
};

const currentStatusText = computed(() => {
  switch (overallStatus.value) {
    case 'wait': return '待启动';
    case 'queued':
    case 'pending': return '排队等待处理...';
    case 'running': return `第 ${currentStepIndex.value} 步处理中...`;
    case 'pending_review': return '等待人工审核';
    case 'duplicate_warning': return '相似文档待确认';
    case 'completed': return '入库完成';
    case 'failed': return String(errorMessage.value).includes('FORMAT_INVALID') ? '格式校验未通过（请整理后重传）' : '处理异常';
    case 'cancelled': return '已取消入库';
    default: return overallStatus.value;
  }
});

const statusBadgeClass = computed(() => {
  switch (overallStatus.value) {
    case 'completed': return 'bg-emerald-50 text-emerald-700 border-emerald-200/80';
    case 'queued':
    case 'pending': return 'bg-indigo-50 text-indigo-700 border-indigo-200/80 animate-pulse';
    case 'pending_review': return 'bg-amber-50 text-amber-800 border-amber-200/80 animate-pulse';
    case 'duplicate_warning': return 'bg-orange-50 text-orange-800 border-orange-200/80 animate-pulse';
    case 'running': return 'bg-blue-50 text-blue-700 border-blue-200/80';
    case 'failed': return 'bg-rose-50 text-rose-700 border-rose-200/80';
    default: return 'bg-slate-100 text-slate-600 border-slate-200';
  }
});

const overallStatusDotClass = computed(() => {
  switch (overallStatus.value) {
    case 'completed': return 'bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.5)]';
    case 'queued':
    case 'pending': return 'bg-indigo-500 animate-pulse shadow-[0_0_6px_rgba(99,102,241,0.5)]';
    case 'running': return 'bg-blue-500 animate-pulse shadow-[0_0_6px_rgba(59,130,246,0.5)]';
    case 'pending_review': return 'bg-amber-500 animate-ping';
    case 'duplicate_warning': return 'bg-orange-500 animate-ping';
    case 'failed': return 'bg-rose-500';
    default: return 'bg-slate-400';
  }
});

// 更新节点状态
const setStepStatus = (index: number, status: PipelineStepStatus, detail?: string) => {
  const step = steps.value.find((s) => s.index === index);
  if (step) {
    step.status = status;
    if (detail !== undefined) {
      step.detail = detail;
    } else if (status === 'process') {
      step.detail = `正在执行：${step.name}...`;
    } else if (status === 'finish') {
      step.detail = '已完成';
    } else if (status === 'skipped') {
      step.detail = '已跳过人工审核';
    } else if (status === 'wait') {
      step.detail = '';
    }
  }
};

const isTerminalStatus = (st?: string): boolean => {
  return st === 'completed' || st === 'active' || st === 'failed' || st === 'cancelled';
};

const getSkippedReviewDetail = (): string => {
  return pendingReviews.value.length > 0 ? '已跳过人工审核' : '无图表项，无需审核';
};

const applySkippedStepNormalization = (docOverallStatus?: string) => {
  if (docOverallStatus === 'completed' || docOverallStatus === 'active' || currentStepIndex.value >= 8) {
    const step7 = steps.value.find((s) => s.index === 7);
    if (step7 && (step7.status === 'wait' || step7.status === 'skipped')) {
      setStepStatus(7, 'skipped', getSkippedReviewDetail());
    }
  }
};

/** 从后端快照恢复 */
const syncFromSnapshot = async (): Promise<string | null> => {
  if (!props.docId) return null;
  try {
    const snap = await documentsApi.getPipelineStatus(props.docId);
    overallStatus.value = snap.overall_status;
    currentStepIndex.value = snap.current_step_index || 1;
    if (snap.file_name) {
      emit('fileNameChange', snap.file_name);
    }

    if (snap.steps && snap.steps.length > 0) {
      snap.steps.forEach((s: any) => {
        const idx = s.step_index ?? s.index;
        const st = normalizeStatus(s.status);
        const detail = s.error_message || s.detail;
        setStepStatus(idx, st, detail);
        if (st === 'error' && detail) {
          errorMessage.value = detail;
        }
      });
    }
    pendingReviews.value = Array.isArray(snap.pending_reviews) ? snap.pending_reviews : [];
    applySkippedStepNormalization(snap.overall_status);

    if (snap.matched_doc_id) {
      matchedDocId.value = snap.matched_doc_id;
    }
    if (snap.requires_review && snap.pending_reviews?.length > 0) {
      emit('reviewRequired', snap.pending_reviews);
    }
    if (snap.overall_status === 'duplicate_warning' || snap.is_duplicate_warning) {
      emit('duplicateWarning', snap.matched_doc_id);
    }
    const statusLabelMap: Record<string, string> = {
      completed: '已完成',
      active: '已完成',
      queued: '排队中',
      running: '处理中',
      pending_review: '待人工审核',
      duplicate_warning: '待确认相似文档',
      failed: '异常',
      cancelled: '已取消',
    };
    addLog('状态', 'bg-slate-100 text-slate-700', `已同步文档进度 · 当前状态: ${statusLabelMap[snap.overall_status] || snap.overall_status}`);
    return snap.overall_status;
  } catch (err) {
    console.warn('同步快照失败:', err);
    addLog('状态', 'bg-rose-100 text-rose-700', '获取文档处理进度失败');
    return null;
  }
};

/** 连接或重新连接 SSE */
const connectSSE = () => {
  if (!props.docId) return;
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }

  addLog('进度流', 'bg-blue-100 text-blue-700', '已连接实时进度通知');

  eventSource = documentsApi.createPipelineEventSource(
    props.docId,
    (eventName, data) => {
      if (eventName === 'initial_state') {
        if (data.overall_status) overallStatus.value = data.overall_status;
        if (data.current_step_index) currentStepIndex.value = data.current_step_index;
        if (data.matched_doc_id) {
          matchedDocId.value = data.matched_doc_id;
        }
        if (data.steps) {
          data.steps.forEach((s: any) => {
            const idx = s.step_index ?? s.index;
            const st = normalizeStatus(s.status);
            const detail = s.error_message || s.detail;
            setStepStatus(idx, st, detail);
            if (st === 'error' && detail) {
              errorMessage.value = detail;
            }
          });
        }
        if (data.pending_reviews) {
          pendingReviews.value = data.pending_reviews;
        }
        applySkippedStepNormalization(data.overall_status);
        if (data.overall_status === 'duplicate_warning' || data.is_duplicate_warning) {
          overallStatus.value = 'duplicate_warning';
          emit('duplicateWarning', data.matched_doc_id);
        }

        if (isTerminalStatus(data.overall_status)) {
          if (eventSource) {
            eventSource.close();
            eventSource = null;
          }
        }
      } else if (eventName === 'pipeline_queued') {
        overallStatus.value = 'queued';
        errorMessage.value = '';
        addLog('排队', 'bg-indigo-100 text-indigo-700', data.message || '文档已提交，正在排队等待处理...');
      } else if (eventName === 'step_started') {
        const idx = data.step_index ?? data.step;
        currentStepIndex.value = idx;
        const msg = data.detail || `正在执行第 ${idx} 步 [${data.name}]`;
        setStepStatus(idx, 'process', msg);
        for (let i = 1; i < idx; i++) {
          if (i === 7 && idx > 7) {
            const step7 = steps.value.find((s) => s.index === 7);
            if (step7 && step7.status === 'wait') {
              setStepStatus(7, 'skipped', getSkippedReviewDetail());
              continue;
            }
          }
          const prev = steps.value.find((s) => s.index === i);
          if (prev && prev.status !== 'skipped') {
            setStepStatus(i, 'finish');
          }
        }
        addLog(`步骤 ${idx}`, 'bg-blue-100 text-blue-700', msg);
      } else if (eventName === 'step_progress') {
        const idx = data.step_index ?? data.step;
        if (idx) {
          currentStepIndex.value = idx;
          const st = normalizeStatus(data.status || 'running');
          if (st === 'process') {
            errorMessage.value = '';
            if (overallStatus.value === 'queued' || overallStatus.value === 'wait' || overallStatus.value === 'pending') {
              overallStatus.value = 'running';
            }
          }
          setStepStatus(idx, st, data.error || data.detail);
          if (st === 'finish') {
            for (let i = 1; i <= idx; i++) {
              if (i === 7 && idx > 7) {
                const step7 = steps.value.find((s) => s.index === 7);
                if (step7 && step7.status === 'wait') {
                  setStepStatus(7, 'skipped', getSkippedReviewDetail());
                  continue;
                }
              }
              const sItem = steps.value.find((s) => s.index === i);
              if (sItem && sItem.status !== 'skipped') {
                setStepStatus(i, 'finish');
              }
            }
          }
          if (data.detail) {
            addLog(`步骤 ${idx}`, 'bg-blue-50 text-blue-600', data.detail);
          }
        }
      } else if (eventName === 'step_completed') {
        const idx = data.step_index ?? data.step;
        const msg = data.detail || '已完成';
        setStepStatus(idx, 'finish', msg);
        addLog(`步骤 ${idx}`, 'bg-emerald-100 text-emerald-700', `${msg}`);
      } else if (eventName === 'step_failed') {
        const idx = data.step_index ?? data.step ?? currentStepIndex.value;
        const errMsg = data.error || data.detail || '步骤执行失败';
        currentStepIndex.value = idx;
        for (let i = 1; i < idx; i++) {
          const prev = steps.value.find((s) => s.index === i);
          if (prev && prev.status !== 'skipped') {
            setStepStatus(i, 'finish');
          }
        }
        setStepStatus(idx, 'error', errMsg);
        overallStatus.value = 'failed';
        errorMessage.value = errMsg;
        emit('failed', errMsg);
        addLog('失败', 'bg-rose-100 text-rose-700', `第 ${idx} 步异常: ${errMsg}`);
      } else if (eventName === 'review_required') {
        overallStatus.value = 'pending_review';
        setStepStatus(7, 'process', '待人工审核');
        const items = data.items || data.pending_reviews || [];
        pendingReviews.value = items;
        emit('reviewRequired', items);
        addLog('审核', 'bg-amber-100 text-amber-800', `等待人工核对（共 ${items.length} 项）`);
      } else if (eventName === 'review_completed') {
        setStepStatus(7, 'finish', '人工审核已确认');
        const updatedItems = data.items || data.pending_reviews;
        if (Array.isArray(updatedItems) && updatedItems.length > 0) {
          pendingReviews.value = updatedItems;
        } else if (pendingReviews.value.length > 0) {
          pendingReviews.value = pendingReviews.value.map((item) => ({
            ...item,
            status: item.status === 'pending' ? 'approved' : item.status,
          }));
        }
        addLog('审核', 'bg-emerald-100 text-emerald-700', '人工审核已确认，继续完成入库');
      } else if (eventName === 'duplicate_warning') {
        overallStatus.value = 'duplicate_warning';
        if (data.matched_doc_id) {
          matchedDocId.value = data.matched_doc_id;
        }
        setStepStatus(11, 'warning', data.message || '检测到相似文档待确认');
        emit('duplicateWarning', data.matched_doc_id);
        addLog('提示', 'bg-orange-100 text-orange-800', `检测到相似文档 (ID: ${data.matched_doc_id})`);
      } else if (eventName === 'pipeline_resumed') {
        overallStatus.value = 'running';
        errorMessage.value = '';
        const idx = data.from_step ?? currentStepIndex.value;
        currentStepIndex.value = idx;
        setStepStatus(idx, 'process', data.detail || '继续处理中');
        for (let i = idx + 1; i <= 11; i++) {
          setStepStatus(i, 'wait', '');
        }
        addLog('恢复', 'bg-blue-100 text-blue-700', `继续执行第 ${idx} 步`);
      } else if (eventName === 'pipeline_completed') {
        overallStatus.value = 'completed';
        steps.value.forEach((s) => {
          if (s.index === 7 && s.status === 'wait') {
            s.status = 'skipped';
            s.detail = getSkippedReviewDetail();
          } else if (s.status !== 'skipped') {
            s.status = 'finish';
          }
        });
        emit('completed', props.docId);
        addLog('完成', 'bg-emerald-100 text-emerald-700', '文档已成功入库');
        if (eventSource) {
          eventSource.close();
          eventSource = null;
        }
      } else if (eventName === 'pipeline_cancelled') {
        overallStatus.value = 'cancelled';
        addLog('取消', 'bg-slate-100 text-slate-600', data.message || '已取消本次入库');
        if (eventSource) {
          eventSource.close();
          eventSource = null;
        }
      } else if (eventName === 'pipeline_failed') {
        const idx = data.step_index ?? data.step ?? currentStepIndex.value;
        const errMsg = data.error || data.detail || '处理执行异常';
        currentStepIndex.value = idx;
        for (let i = 1; i < idx; i++) {
          const prev = steps.value.find((s) => s.index === i);
          if (prev && prev.status !== 'skipped') {
              setStepStatus(i, 'finish');
            }
          }
        setStepStatus(idx, 'error', errMsg);
        overallStatus.value = 'failed';
        errorMessage.value = errMsg;
        emit('failed', errMsg);
        addLog('失败', 'bg-rose-100 text-rose-700', `处理中止: ${errMsg}`);
        if (eventSource) {
          eventSource.close();
          eventSource = null;
        }
      }
    },
    (err) => {
      // 若非活跃流水线（例如服务重启后历史文档返回 404），主动关闭 EventSource 杜绝无限自动重连刷 404
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      if (!isTerminalStatus(overallStatus.value)) {
        console.warn('进度流连接已断开:', err);
      }
    }
  );
};

const reconnect = async () => {
  if (isReconnecting.value) return;
  isReconnecting.value = true;
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  const snapStatus = await syncFromSnapshot();
  // 仅在文档处于非终态（排队、处理中、待审核、待确认重复）时才建立 SSE 实时监听
  if (snapStatus && !isTerminalStatus(snapStatus)) {
    connectSSE();
  }
  setTimeout(() => {
    isReconnecting.value = false;
  }, 400);
};

watch(
  () => props.docId,
  (newId) => {
    if (newId && props.autoConnect !== false) {
      reconnect();
    }
  },
  { immediate: true }
);

onUnmounted(() => {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
});

defineExpose({
  reconnect,
  syncFromSnapshot,
  handleRetryCurrentStep,
});
</script>
