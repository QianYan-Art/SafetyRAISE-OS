import type {
  GenerateInputFromUploadResponse,
  GenerateReportResponse,
} from "./types";

function prettyJson(payload: unknown): string {
  return JSON.stringify(payload, null, 2);
}

function formatDurationLabel(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "0.00 秒";
  }
  return `${seconds.toFixed(2)} 秒`;
}

function truncateText(value: string, maxLength = 120): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, maxLength)}...`;
}

function joinWindowsPath(basePath: string, fileName: string): string {
  return `${basePath.replace(/[\\/]+$/, "")}\\${fileName}`;
}

export function getInputProgressLabels(files: File[]): string[] {
  const hasImage = files.some((file) => file.type.startsWith("image/"));
  const hasVideo = files.some((file) => file.type.startsWith("video/"));
  const isVideoMode = hasVideo || (hasImage && hasVideo);

  if (isVideoMode) {
    return ["正在执行 YOLO 轨迹识别", "正在抽取关键帧并整理分组证据", "正在生成事故信息草稿"];
  }

  return ["正在解析图片内容", "正在按分组整理事故材料", "正在生成事故信息草稿"];
}

export function formatGuidanceSummaryMarkdown(guidance: Record<string, unknown>): string {
  const entries = Object.entries(guidance).slice(0, 5);
  if (!entries.length) {
    return "";
  }
  return [
    "### 专家指导意见速览",
    ...entries.map(([key, value]) => {
      const rendered = typeof value === "string" ? truncateText(value, 150) : truncateText(prettyJson(value), 150);
      return `- **${key}**：${rendered}`;
    }),
  ].join("\n");
}

export function formatKnowledgeSnippetsMarkdown(
  snippets: GenerateReportResponse["knowledge_snippets"],
  retrievalMeta: Record<string, unknown>,
): string {
  const safeSnippets = snippets ?? [];
  if (!safeSnippets.length) {
    return "";
  }
  const initialQuery = String(retrievalMeta.initial_query ?? retrievalMeta.last_query ?? "").trim();
  return [
    "### 首轮知识库片段（节选）",
    ...(initialQuery ? [`- 检索主题：${truncateText(initialQuery, 90)}`] : []),
    ...safeSnippets.slice(0, 4).flatMap((snippet, index) => {
      const title = snippet.title || snippet.id || `片段 ${index + 1}`;
      const excerpt = truncateText(String(snippet.content || "无正文片段"), 160);
      const score = typeof snippet.score === "number" ? `，匹配分 ${snippet.score.toFixed(3)}` : "";
      const meta = [
        snippet.citation ? `引用：\`${snippet.citation}\`` : "",
        snippet.category ? `类别：${snippet.category}` : "",
      ].filter(Boolean).join("，");
      return [
        `- **${title}**${score}`,
        `  ${excerpt}`,
        ...(meta ? [`  ${meta}`] : []),
      ];
    }),
  ].join("\n");
}

export function formatAgenticRoundsMarkdown(rounds: GenerateReportResponse["agentic_retrieval_rounds"]): string {
  const safeRounds = rounds ?? [];
  if (!safeRounds.length) {
    return "";
  }
  return [
    "### Agentic RAG 新增片段（节选）",
    ...safeRounds.slice(0, 3).flatMap((round) => {
      const safeSnippets = round.snippets ?? [];
      return [
        `- **第 ${round.round} 轮补充检索**：${round.query}`,
        `  触发原因：${truncateText(round.reason, 96)}`,
        `  返回片段：${round.returned_count} 条`,
        ...safeSnippets.slice(0, 2).map((snippet, index) => {
          const title = snippet.title || snippet.id || `片段 ${index + 1}`;
          const excerpt = truncateText(String(snippet.content || "无正文片段"), 140);
          const score = typeof snippet.score === "number" ? `（匹配分 ${snippet.score.toFixed(3)}）` : "";
          return `  - ${title}${score}：${excerpt}`;
        }),
      ];
    }),
  ].join("\n");
}

export function formatLinkedFilesMarkdown(outputDir: string, workspaceDir?: string): string {
  return [
    "### 本轮新增关键产物",
    ...(workspaceDir ? [`- 输入工作区：\`${workspaceDir}\``] : []),
    `- 报告目录：\`${outputDir}\``,
    `- 指导意见：\`${joinWindowsPath(outputDir, "guidance.json")}\``,
    `- 报告正文：\`${joinWindowsPath(outputDir, "report.md")}\``,
    `- 结构化报告：\`${joinWindowsPath(outputDir, "report.json")}\``,
    `- 运行日志：\`${joinWindowsPath(outputDir, "run_log.json")}\``,
  ].join("\n");
}

export function formatYoloPreviewMarkdown(
  preview: GenerateInputFromUploadResponse["yolo_summary_preview"],
  frameManifest: GenerateInputFromUploadResponse["frame_manifest"],
): string {
  const safeVideos = preview?.videos ?? [];
  const safeFrameManifest = frameManifest ?? [];
  if (!preview || !safeVideos.length) {
    return "";
  }

  const lines = [
    "### YOLO 轨迹摘要预览",
    `- 视频来源：${preview.video_source_count} 段`,
    ...(preview.image_source_count > 0 ? [`- 同轮补充图片：${preview.image_source_count} 组`] : []),
    `- 送入视觉模型的图片/关键帧：${safeFrameManifest.length} 张`,
  ];

  safeVideos.slice(0, 2).forEach((video) => {
    const trackHighlights = video.track_highlights ?? [];
    const eventHighlights = video.event_highlights ?? [];
    const classSummary = Object.entries(video.class_counts)
      .slice(0, 4)
      .map(([label, count]) => `${label} ${count}`)
      .join("，");
    lines.push(`#### ${video.source_name}`);
    if (video.category_label) {
      lines.push(`- 所属分组：${video.category_label}${video.category_subtitle ? ` / ${video.category_subtitle}` : ""}`);
    }
    lines.push(`- 时长 ${formatDurationLabel(video.duration_seconds)}，共 ${video.frame_count} 帧，FPS ${video.fps}`);
    lines.push(`- 轨迹对象 ${video.unique_track_count} 个，总检测 ${video.total_detections} 次`);
    if (classSummary) {
      lines.push(`- 类别分布：${classSummary}`);
    }
    if (trackHighlights.length) {
      lines.push("- 轨迹亮点：");
      trackHighlights.forEach((track) => {
        lines.push(
          `  - 轨迹 #${track.track_id}（${track.class_name}），均值速度 ${track.mean_speed_px_s} px/s，最高速度 ${track.max_speed_px_s} px/s，均值加速度 ${track.mean_abs_acceleration_px_s2} px/s²，最高加速度 ${track.max_abs_acceleration_px_s2} px/s²`,
        );
      });
    }
    if (eventHighlights.length) {
      lines.push("- 事件候选：");
      eventHighlights.forEach((event) => {
        lines.push(`  - ${formatDurationLabel(event.timestamp_seconds)} / 帧 ${event.frame} / 事件分 ${event.event_score} / ${event.reason} / 目标数 ${event.object_count}`);
      });
    }
  });
  return lines.join("\n");
}
