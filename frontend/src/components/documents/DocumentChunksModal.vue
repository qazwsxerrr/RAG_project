<template>
  <Teleport to="body">
    <div 
      v-if="visible" 
      class="fixed inset-y-0 right-0 left-0 md:left-60 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-xs p-4 sm:p-6 animate-in fade-in duration-200" 
      @click.self="$emit('close')"
    >
      <div 
        class="bg-white border border-slate-200 rounded-2xl shadow-2xl max-w-5xl w-full p-6 text-slate-800 flex flex-col max-h-[92vh]" 
        @click.stop
      >
        <!-- 弹窗顶栏 -->
        <div class="flex items-center justify-between pb-4 border-b border-slate-100 shrink-0">
          <div>
            <div class="flex items-center gap-3">
              <h3 class="text-base font-bold text-slate-900 flex items-center gap-2">
                <svg class="w-4 h-4 text-blue-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                </svg>
                文档切片资产详情
              </h3>
              <span class="text-xs text-slate-400 font-mono">
                共 {{ chunks.length }} 个切片
              </span>
              <!-- 切片类型统计徽标 -->
              <div v-if="stats.tables > 0 || stats.flowcharts > 0 || stats.codes > 0 || stats.images > 0" class="hidden sm:flex items-center gap-1.5 text-[11px]">
                <span v-if="stats.tables > 0" class="px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200 font-medium">
                  {{ stats.tables }} 表格
                </span>
                <span v-if="stats.flowcharts > 0" class="px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 font-medium">
                  {{ stats.flowcharts }} 流程图
                </span>
                <span v-if="stats.codes > 0" class="px-2 py-0.5 rounded-full bg-sky-50 text-sky-700 border border-sky-200 font-medium">
                  {{ stats.codes }} 代码块
                </span>
                <span v-if="stats.images > 0" class="px-2 py-0.5 rounded-full bg-purple-50 text-purple-700 border border-purple-200 font-medium">
                  {{ stats.images }} 配图
                </span>
              </div>
            </div>
            <p class="text-xs text-slate-500 mt-1 font-mono truncate max-w-2xl">
              文档: <span class="text-slate-700 font-medium">{{ fileName }}</span>
            </p>
          </div>
          <button 
            @click="$emit('close')" 
            class="text-slate-400 hover:text-slate-700 w-8 h-8 rounded-lg flex items-center justify-center hover:bg-slate-100 text-xl transition-colors"
            title="关闭弹窗"
          >
            &times;
          </button>
        </div>

        <!-- 切片列表滚动区域 -->
        <div class="overflow-y-auto py-4 space-y-4 flex-1 pr-1.5 custom-scrollbar">
          <div v-if="loading" class="text-center py-16 text-xs text-slate-500 flex flex-col items-center gap-3">
            <svg class="w-6 h-6 animate-spin text-blue-600" viewBox="0 0 24 24" fill="none" stroke="currentColor">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
            </svg>
            <span>正在检索切片多模态资产与向量元数据...</span>
          </div>

          <div v-else-if="chunks.length === 0" class="text-center py-16 text-xs text-slate-400">
            暂未找到切片数据或文档尚在解析流水线中
          </div>

          <div 
            v-else 
            v-for="chunk in chunks" 
            :key="chunk.chunk_index" 
            class="border border-slate-200 rounded-xl p-4 bg-white hover:border-slate-300 transition-all space-y-3 shadow-xs"
          >
            <!-- 切片标头与元数据 -->
            <div class="flex flex-wrap items-center justify-between gap-2 text-xs pb-2.5 border-b border-slate-100">
              <div class="flex items-center flex-wrap gap-2">
                <span class="font-bold text-slate-800 font-mono">{{ chunk.chunk_label || `第 ${chunk.chunk_index} 片` }}</span>
                <span class="text-slate-300">·</span>
                <span class="text-slate-600 bg-slate-100 px-2 py-0.5 rounded text-[11px]">
                  原文档第 {{ chunk.display_page || (chunk.page_idx + 1) }} 页
                </span>

                <!-- 类型标签 -->
                <span v-if="isFlowchart(chunk)" class="px-2 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-200 text-[11px] font-medium flex items-center gap-1">
                  <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>流程图 (Mermaid)
                </span>
                <span v-else-if="isTable(chunk)" class="px-2 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200 text-[11px] font-medium flex items-center gap-1">
                  <span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span>结构化表格
                </span>
                <span v-else-if="isCode(chunk)" class="px-2 py-0.5 rounded bg-sky-50 text-sky-700 border border-sky-200 text-[11px] font-medium flex items-center gap-1">
                  <span class="w-1.5 h-1.5 rounded-full bg-sky-500"></span>代码块
                </span>
                <span v-else-if="isImage(chunk)" class="px-2 py-0.5 rounded bg-purple-50 text-purple-700 border border-purple-200 text-[11px] font-medium flex items-center gap-1">
                  <span class="w-1.5 h-1.5 rounded-full bg-purple-500"></span>文档配图
                </span>
              </div>

              <div class="text-[11px] text-slate-400 font-mono flex items-center gap-2">
                <span>字符数: {{ chunk.content.length }}</span>
                <span>·</span>
                <span>稀疏Token: {{ chunk.sparse_token_count || 0 }}</span>
              </div>
            </div>

            <!-- 面包屑层级导航 -->
            <div v-if="chunk.breadcrumb && chunk.breadcrumb.length > 0" class="text-[11px] text-slate-500 flex items-center gap-1 flex-wrap">
              <span class="text-slate-400">章节层级:</span>
              <span v-for="(b, idx) in chunk.breadcrumb" :key="idx" class="flex items-center gap-1">
                <span class="text-slate-600 font-medium">{{ b }}</span>
                <span v-if="idx < chunk.breadcrumb.length - 1" class="text-slate-300">/</span>
              </span>
            </div>

            <!-- ===================== 分区一：原文档富媒体内容 (渲染后) ===================== -->
            <!-- 1. 流程图切片渲染 (Mermaid SVG / 源码 / 切图) -->
            <div v-if="isFlowchart(chunk)" class="rounded-xl border border-emerald-200/80 bg-emerald-50/20 p-3 space-y-2.5">
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-1.5 text-xs font-semibold text-emerald-800">
                  <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <rect x="3" y="3" width="6" height="6" rx="1"/>
                    <rect x="15" y="15" width="6" height="6" rx="1"/>
                    <path d="M6 9v3a3 3 0 0 0 3 3h6"/>
                  </svg>
                  <span>原文档流程图 (已矢量化渲染)</span>
                </div>

                <!-- 切换视图 Tab -->
                <div class="flex items-center gap-1 bg-white p-0.5 rounded-lg border border-slate-200 text-[11px]">
                  <button 
                    @click="setTab(chunk.chunk_index, 'render')" 
                    :class="getTab(chunk.chunk_index) === 'render' ? 'bg-emerald-600 text-white font-medium' : 'text-slate-600 hover:text-slate-900'"
                    class="px-2 py-0.5 rounded transition-colors"
                  >
                    矢量图 (SVG)
                  </button>
                  <button 
                    @click="setTab(chunk.chunk_index, 'source')" 
                    :class="getTab(chunk.chunk_index) === 'source' ? 'bg-emerald-600 text-white font-medium' : 'text-slate-600 hover:text-slate-900'"
                    class="px-2 py-0.5 rounded transition-colors"
                  >
                    Mermaid 语法
                  </button>
                  <button 
                    v-if="chunk.asset_url" 
                    @click="setTab(chunk.chunk_index, 'asset')" 
                    :class="getTab(chunk.chunk_index) === 'asset' ? 'bg-emerald-600 text-white font-medium' : 'text-slate-600 hover:text-slate-900'"
                    class="px-2 py-0.5 rounded transition-colors"
                  >
                    原版切图
                  </button>
                </div>
              </div>

              <!-- Tab 1: 矢量 SVG 流程图展示 -->
              <div v-show="getTab(chunk.chunk_index) === 'render'" class="bg-white rounded-lg border border-slate-200 p-4 min-h-[120px] flex items-center justify-center">
                <div 
                  v-if="mermaidSvgs[chunk.chunk_index]" 
                  class="mermaid-svg-container w-full overflow-x-auto flex justify-center" 
                  v-html="mermaidSvgs[chunk.chunk_index]"
                ></div>
                <div v-else-if="mermaidErrors[chunk.chunk_index]" class="w-full p-3 bg-amber-50 border border-amber-200 rounded-lg text-amber-800 text-xs space-y-1">
                  <div class="font-semibold flex items-center gap-1.5">
                    <svg class="w-4 h-4 text-amber-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                    <span>流程图矢量渲染遇到问题，已降级展示原始语法：</span>
                  </div>
                  <pre class="bg-white p-2.5 rounded border border-amber-200 font-mono text-[11px] overflow-x-auto text-slate-800">{{ getCleanMermaid(chunk.table_html) }}</pre>
                </div>
                <div v-else class="text-xs text-slate-400 py-6 flex items-center gap-2">
                  <svg class="w-4 h-4 animate-spin text-emerald-600" viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
                  <span>Mermaid 矢量流程图计算渲染中...</span>
                </div>
              </div>

              <!-- Tab 2: Mermaid 语法纯文本 -->
              <div v-show="getTab(chunk.chunk_index) === 'source'" class="relative">
                <pre class="bg-slate-900 text-emerald-400 p-3.5 rounded-lg text-xs font-mono overflow-x-auto max-h-64 leading-relaxed selection:bg-emerald-900 selection:text-white">{{ getCleanMermaid(chunk.table_html) }}</pre>
                <button 
                  @click="copyText(getCleanMermaid(chunk.table_html), chunk.chunk_index)" 
                  class="absolute top-2.5 right-2.5 bg-slate-800/80 hover:bg-slate-700 text-slate-300 hover:text-white text-[11px] px-2 py-1 rounded flex items-center gap-1 transition-colors backdrop-blur-xs"
                >
                  <svg v-if="copiedId === chunk.chunk_index" class="w-3.5 h-3.5 text-emerald-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
                  <svg v-else class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                  <span>{{ copiedId === chunk.chunk_index ? '已复制' : '复制语法' }}</span>
                </button>
              </div>

              <!-- Tab 3: 原版切图展示 (若存在) -->
              <div v-show="getTab(chunk.chunk_index) === 'asset'" v-if="chunk.asset_url" class="relative group bg-white border border-slate-200 rounded-lg p-3 flex justify-center">
                <img 
                  :src="chunk.asset_url" 
                  alt="流程图原文档截取" 
                  class="max-h-72 object-contain rounded cursor-zoom-in transition-transform duration-150 group-hover:scale-[1.01]" 
                  @click="openLightbox(chunk.asset_url)" 
                  loading="lazy"
                />
                <div class="absolute bottom-3 right-3 opacity-0 group-hover:opacity-100 transition-opacity bg-slate-900/80 text-white text-[11px] px-2.5 py-1 rounded flex items-center gap-1 pointer-events-none">
                  <svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg>
                  <span>点击放大</span>
                </div>
              </div>
            </div>

            <!-- 2. 表格切片渲染 (结构化表格 / 原版切图) -->
            <div v-else-if="isTable(chunk)" class="rounded-xl border border-amber-200/80 bg-amber-50/20 p-3 space-y-2.5">
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-1.5 text-xs font-semibold text-amber-900">
                  <svg class="w-3.5 h-3.5 text-amber-700" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/><path d="M9 3v18"/><path d="M15 3v18"/>
                  </svg>
                  <span>原文档表格 (结构化渲染)</span>
                </div>

                <!-- 切换视图 Tab (若同时有结构化和原切图) -->
                <div v-if="chunk.table_html && chunk.asset_url" class="flex items-center gap-1 bg-white p-0.5 rounded-lg border border-slate-200 text-[11px]">
                  <button 
                    @click="setTab(chunk.chunk_index, 'render')" 
                    :class="getTab(chunk.chunk_index) === 'render' ? 'bg-amber-600 text-white font-medium' : 'text-slate-600 hover:text-slate-900'"
                    class="px-2 py-0.5 rounded transition-colors"
                  >
                    结构化表格
                  </button>
                  <button 
                    @click="setTab(chunk.chunk_index, 'asset')" 
                    :class="getTab(chunk.chunk_index) === 'asset' ? 'bg-amber-600 text-white font-medium' : 'text-slate-600 hover:text-slate-900'"
                    class="px-2 py-0.5 rounded transition-colors"
                  >
                    原版文档截取
                  </button>
                </div>
              </div>

              <!-- 结构化 HTML 表格内容 -->
              <div 
                v-if="chunk.table_html && getTab(chunk.chunk_index) === 'render'" 
                class="rendered-table-container bg-white border border-slate-200 rounded-lg p-2.5 overflow-x-auto max-h-72" 
                v-html="chunk.table_html"
              ></div>

              <!-- 原版切图内容 (如果只有切图或切换到了原切图视图) -->
              <div 
                v-else-if="chunk.asset_url" 
                class="relative group bg-white border border-slate-200 rounded-lg p-3 flex justify-center"
              >
                <img 
                  :src="chunk.asset_url" 
                  alt="表格原文档截取" 
                  class="max-h-72 object-contain rounded cursor-zoom-in transition-transform duration-150 group-hover:scale-[1.01]" 
                  @click="openLightbox(chunk.asset_url)" 
                  loading="lazy"
                />
                <div class="absolute bottom-3 right-3 opacity-0 group-hover:opacity-100 transition-opacity bg-slate-900/80 text-white text-[11px] px-2.5 py-1 rounded flex items-center gap-1 pointer-events-none">
                  <svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg>
                  <span>点击放大</span>
                </div>
              </div>
            </div>

            <!-- 3. 代码块切片渲染 (源码高亮卡片 / 一键复制) -->
            <div v-else-if="isCode(chunk)" class="rounded-xl border border-sky-200/80 bg-sky-50/20 p-3 space-y-2.5">
              <div class="flex items-center justify-between text-xs">
                <div class="flex items-center gap-1.5 font-semibold text-sky-900">
                  <svg class="w-3.5 h-3.5 text-sky-700" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
                  <span>原文档代码块</span>
                  <span class="px-1.5 py-0.5 bg-sky-100 text-sky-700 rounded text-[10px] font-mono uppercase font-bold">
                    {{ getCodeDetails(chunk).lang }}
                  </span>
                </div>

                <button 
                  @click="copyText(getCodeDetails(chunk).code, chunk.chunk_index)" 
                  class="bg-white hover:bg-slate-100 border border-slate-200 text-slate-700 text-[11px] px-2.5 py-1 rounded-lg flex items-center gap-1 transition-colors shadow-2xs font-medium"
                >
                  <svg v-if="copiedId === chunk.chunk_index" class="w-3.5 h-3.5 text-emerald-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
                  <svg v-else class="w-3.5 h-3.5 text-slate-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                  <span>{{ copiedId === chunk.chunk_index ? '已复制代码' : '复制代码' }}</span>
                </button>
              </div>

              <div class="relative">
                <pre class="bg-slate-900 text-slate-100 p-3.5 rounded-lg text-xs font-mono overflow-x-auto max-h-64 leading-relaxed selection:bg-sky-900 selection:text-white">{{ getCodeDetails(chunk).code }}</pre>
              </div>
            </div>

            <!-- 4. 配图切片渲染 (原图内嵌展示 / 放大灯箱) -->
            <div v-else-if="isImage(chunk)" class="rounded-xl border border-purple-200/80 bg-purple-50/20 p-3 space-y-2.5">
              <div class="flex items-center justify-between text-xs">
                <div class="flex items-center gap-1.5 font-semibold text-purple-900">
                  <svg class="w-3.5 h-3.5 text-purple-700" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>
                  </svg>
                  <span>原文档配图 (原图预览)</span>
                </div>
                <a 
                  :href="chunk.asset_url" 
                  target="_blank" 
                  class="text-[11px] text-purple-700 hover:text-purple-800 font-medium flex items-center gap-1"
                >
                  <span>在新标签打开</span>
                  <svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                </a>
              </div>

              <div class="relative group bg-white border border-slate-200 rounded-lg p-3 flex justify-center overflow-hidden">
                <img 
                  :src="chunk.asset_url" 
                  :alt="chunk.chunk_label" 
                  class="max-h-72 object-contain rounded cursor-zoom-in transition-transform duration-150 group-hover:scale-[1.01]" 
                  @click="openLightbox(chunk.asset_url)" 
                  loading="lazy"
                />
                <div class="absolute bottom-3 right-3 opacity-0 group-hover:opacity-100 transition-opacity bg-slate-900/80 text-white text-[11px] px-2.5 py-1 rounded flex items-center gap-1 pointer-events-none">
                  <svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg>
                  <span>点击全屏放大</span>
                </div>
              </div>
            </div>

            <!-- ===================== 分区二：AI 语义检索摘要 (向量化内容) ===================== -->
            <div class="space-y-1.5 pt-1">
              <div class="flex items-center justify-between text-[11px] font-semibold text-slate-500 uppercase tracking-wider">
                <span class="flex items-center gap-1.5">
                  <svg class="w-3.5 h-3.5 text-blue-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                    <polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>
                  </svg>
                  AI 语义检索摘要 (向量化与倒排索引文本)
                </span>
                <span class="text-slate-400 font-normal font-mono">
                  用于密集/稀疏语义召回
                </span>
              </div>

              <!-- 语义检索摘要卡片 -->
              <div class="text-xs text-slate-800 leading-relaxed bg-slate-50/70 p-3.5 rounded-lg border border-slate-200/90 max-h-56 overflow-y-auto whitespace-pre-wrap select-text font-sans">
                {{ chunk.content }}
              </div>
            </div>

            <!-- 关联 OSS 产物链接 (非配图类型时显示) -->
            <div v-if="chunk.asset_url && !isImage(chunk)" class="flex items-center justify-end text-xs pt-1">
              <a 
                :href="chunk.asset_url" 
                target="_blank" 
                class="text-blue-600 hover:text-blue-700 font-medium flex items-center gap-1 transition-colors text-[11px]"
              >
                <span>查看原版切图产物 (OSS)</span>
                <svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
              </a>
            </div>
          </div>
        </div>

        <!-- 底部关闭按钮 -->
        <div class="pt-3 border-t border-slate-100 flex items-center justify-between shrink-0">
          <div class="text-xs text-slate-400">
            提示：表格与流程图均已在本地完成高保真矢量与结构化渲染
          </div>
          <button 
            @click="$emit('close')" 
            class="px-5 py-2 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-medium rounded-xl transition-colors"
          >
            关闭
          </button>
        </div>
      </div>
    </div>

    <!-- ===================== 全局图片大图灯箱 (Lightbox) ===================== -->
    <div 
      v-if="lightboxUrl" 
      class="fixed inset-0 z-[9999] bg-slate-950/85 backdrop-blur-md flex items-center justify-center p-4 sm:p-8 animate-in fade-in duration-150" 
      @click="lightboxUrl = null"
    >
      <div class="relative max-w-5xl max-h-[90vh] flex flex-col items-center" @click.stop>
        <!-- 灯箱顶栏工具条 -->
        <div class="absolute -top-10 right-0 flex items-center gap-3 text-white text-xs">
          <a 
            :href="lightboxUrl" 
            target="_blank" 
            class="hover:underline flex items-center gap-1 bg-white/20 px-2.5 py-1 rounded backdrop-blur-xs"
          >
            <span>新标签打开原图</span>
            <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          </a>
          <button 
            @click="lightboxUrl = null" 
            class="hover:text-slate-300 text-2xl font-light w-8 h-8 flex items-center justify-center rounded-full bg-white/10 hover:bg-white/20 transition-colors"
            title="关闭大图"
          >
            &times;
          </button>
        </div>

        <!-- 高清大图 -->
        <img 
          :src="lightboxUrl" 
          alt="大图预览" 
          class="max-w-full max-h-[85vh] object-contain rounded-xl shadow-2xl border border-white/10"
        />
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue';
import mermaid from 'mermaid';
import type { ChunkItem } from '../../types';
import { documentsApi } from '../../api/documents';

const props = defineProps<{
  visible: boolean;
  docId: string;
  fileName: string;
}>();

defineEmits<{
  (e: 'close'): void;
}>();

const chunks = ref<ChunkItem[]>([]);
const loading = ref(false);

// 视图 Tab 切换状态 (chunk_index -> 'render' | 'source' | 'asset')
const activeTabs = ref<Record<number, 'render' | 'source' | 'asset'>>({});

// Mermaid 渲染产物与报错信息
const mermaidSvgs = ref<Record<number, string>>({});
const mermaidErrors = ref<Record<number, string>>({});

// 复制状态与图片灯箱
const copiedId = ref<number | null>(null);
const lightboxUrl = ref<string | null>(null);

// 切片统计
const stats = computed(() => {
  let tables = 0;
  let flowcharts = 0;
  let codes = 0;
  let images = 0;

  for (const c of chunks.value) {
    if (isFlowchart(c)) flowcharts++;
    else if (isTable(c)) tables++;
    else if (isCode(c)) codes++;
    else if (isImage(c)) images++;
  }

  return { tables, flowcharts, codes, images };
});

// 类型判断工具函数
function isFlowchart(chunk: ChunkItem): boolean {
  if (!chunk.is_code) return false;
  const label = chunk.chunk_label || '';
  const html = chunk.table_html || '';
  return label.includes('流程图') || html.includes('language-mermaid') || html.includes('mermaid');
}

function isTable(chunk: ChunkItem): boolean {
  if (chunk.is_table) return true;
  const label = chunk.chunk_label || '';
  const html = chunk.table_html || '';
  return label.includes('表格') || html.includes('<table');
}

function isCode(chunk: ChunkItem): boolean {
  return Boolean(chunk.is_code && !isFlowchart(chunk));
}

function isImage(chunk: ChunkItem): boolean {
  if (isFlowchart(chunk) || isTable(chunk) || isCode(chunk)) return false;
  return Boolean(chunk.asset_url);
}

// 视图 Tab 存取
function getTab(chunkIndex: number): 'render' | 'source' | 'asset' {
  return activeTabs.value[chunkIndex] || 'render';
}

function setTab(chunkIndex: number, tab: 'render' | 'source' | 'asset') {
  activeTabs.value[chunkIndex] = tab;
}

// 提取清洗 Mermaid 代码
function getCleanMermaid(html?: string): string {
  if (!html) return '';
  let decoded = html
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&#39;/g, "'");

  const fenceMatch = decoded.match(/```mermaid\s*([\s\S]*?)```/);
  if (fenceMatch && fenceMatch[1]) {
    return fenceMatch[1].trim();
  }

  const stripped = decoded.replace(/<[^>]+>/g, '').trim();
  const strippedFence = stripped.match(/```mermaid\s*([\s\S]*?)```/);
  if (strippedFence && strippedFence[1]) {
    return strippedFence[1].trim();
  }

  return stripped.replace(/```mermaid/g, '').replace(/```/g, '').trim();
}

// 提取代码块详情 (语言 + 源码)
function getCodeDetails(chunk: ChunkItem): { code: string; lang: string } {
  const html = chunk.table_html || '';
  let decoded = html
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&#39;/g, "'");

  let lang = 'code';
  const langMatch = decoded.match(/class="[^"]*language-([^"\s]+)[^"]*"/i) || chunk.chunk_label?.match(/\(([a-zA-Z0-9_-]+)\)/);
  if (langMatch && langMatch[1]) {
    lang = langMatch[1].toLowerCase();
  }

  const fenceMatch = decoded.match(/```(?:\w+)?\s*([\s\S]*?)```/);
  if (fenceMatch && fenceMatch[1]) {
    return { code: fenceMatch[1].trim(), lang };
  }

  const stripped = decoded.replace(/<[^>]+>/g, '').trim();
  return { code: stripped || chunk.content, lang };
}

// 异步渲染所有 Mermaid 流程图
async function renderAllFlowcharts() {
  const flowchartsToRender = chunks.value.filter(isFlowchart);
  if (flowchartsToRender.length === 0) return;

  mermaid.initialize({
    startOnLoad: false,
    theme: 'neutral',
    securityLevel: 'loose',
    fontFamily: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  });

  for (const chunk of flowchartsToRender) {
    const code = getCleanMermaid(chunk.table_html);
    if (!code) continue;

    const renderId = `mermaid_svg_${chunk.chunk_index}_${Math.random().toString(36).substring(2, 9)}`;
    try {
      const { svg } = await mermaid.render(renderId, code);
      mermaidSvgs.value[chunk.chunk_index] = svg;
    } catch (err: any) {
      console.warn(`Mermaid 渲染失败 (chunk ${chunk.chunk_index}):`, err);
      mermaidErrors.value[chunk.chunk_index] = err?.message || 'Mermaid 流程图解析失败';
    }
  }
}

// 复制纯文本
async function copyText(text: string, id: number) {
  try {
    await navigator.clipboard.writeText(text);
    copiedId.value = id;
    setTimeout(() => {
      if (copiedId.value === id) copiedId.value = null;
    }, 2000);
  } catch (e) {
    console.error('复制失败:', e);
  }
}

// 打开大图预览
function openLightbox(url?: string) {
  if (url) {
    lightboxUrl.value = url;
  }
}

// 键盘快捷键 (ESC 关闭灯箱/弹窗)
function handleKeyDown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    if (lightboxUrl.value) {
      lightboxUrl.value = null;
      e.stopPropagation();
    }
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleKeyDown);
});

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeyDown);
});

// 监听加载切片数据
watch(
  () => props.docId,
  async (newId) => {
    if (newId && props.visible) {
      loading.value = true;
      mermaidSvgs.value = {};
      mermaidErrors.value = {};
      activeTabs.value = {};
      try {
        chunks.value = await documentsApi.getDocumentChunks(newId);
        await nextTick();
        await renderAllFlowcharts();
      } catch (err) {
        console.warn('加载切片失败:', err);
        chunks.value = [];
      } finally {
        loading.value = false;
      }
    }
  },
  { immediate: true }
);
</script>

<style scoped>
/* 结构化表格美化样式 */
.rendered-table-container :deep(table) {
  width: 100% !important;
  border-collapse: collapse !important;
  font-size: 0.8125rem !important; /* 13px */
  line-height: 1.5 !important;
  background-color: #ffffff !important;
}

.rendered-table-container :deep(th) {
  background-color: #f8fafc !important;
  color: #334155 !important;
  font-weight: 600 !important;
  text-align: left !important;
  padding: 0.5rem 0.75rem !important;
  border: 1px solid #e2e8f0 !important;
  white-space: nowrap !important;
}

.rendered-table-container :deep(td) {
  padding: 0.5rem 0.75rem !important;
  border: 1px solid #f1f5f9 !important;
  color: #1e293b !important;
}

.rendered-table-container :deep(tr:nth-child(even) td) {
  background-color: #fafbfc !important;
}

.rendered-table-container :deep(tr:hover td) {
  background-color: #f1f5f9 !important;
}

/* 矢量 Mermaid SVG 自适应容器 */
.mermaid-svg-container :deep(svg) {
  max-width: 100% !important;
  height: auto !important;
  display: block !important;
  margin: 0 auto !important;
}

/* 细滚动条 */
.custom-scrollbar::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}
.custom-scrollbar::-webkit-scrollbar-thumb {
  background-color: #cbd5e1;
  border-radius: 9999px;
}
.custom-scrollbar::-webkit-scrollbar-thumb:hover {
  background-color: #94a3b8;
}
</style>
