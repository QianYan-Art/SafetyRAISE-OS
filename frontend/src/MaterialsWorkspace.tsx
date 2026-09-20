import {
  ArrowRight,
  ChevronRight,
  FileImage,
  FileText,
  Film,
  Grid2X2,
  Image as ImageIcon,
  Info,
  List,
  LockKeyhole,
  MapPin,
  Plus,
  ScanLine,
  Trash2,
  Upload,
  Video,
  X,
} from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type MouseEvent,
} from "react";

import type { PendingUploadGroupState, PendingUploadItem } from "./uploadGroups";
import type { PublicAppConfig } from "./types";
import { useDialogFocus } from "./useDialogFocus";

import "./MaterialsWorkspace.css";

const ALL_GROUP_ID = "all";
const DEFAULT_ADD_GROUP_ID = "accident_overview";

const FRIENDLY_GROUP_LABELS: Readonly<Record<string, string>> = {
  accident_overview: "事故概况",
  accident_videos: "视频",
  injury_photos: "人员损伤",
  vehicle_exterior: "车辆外部",
  vehicle_interior: "车辆内部",
  other_information: "其它信息",
  scene: "现场",
  privacy_photos: "隐私处理",
};

type ViewMode = "grid" | "list";

interface MaterialsWorkspaceProps {
  groups: PendingUploadGroupState[];
  limits: PublicAppConfig["upload_limits"];
  busy: boolean;
  onAdd: (groupId: string) => void;
  onDropFiles: (groupId: string, files: File[]) => void;
  onRemove: (groupId: string, itemId: string) => void;
  onGenerate: () => void;
}

interface MaterialRecord {
  key: string;
  group: PendingUploadGroupState;
  item: PendingUploadItem;
}

interface SelectedMaterial {
  groupId: string;
  itemId: string;
}

interface ObjectUrlEntry {
  file: File;
  url: string | null;
}

function materialKey(groupId: string, itemId: string): string {
  return `${groupId}\u0000${itemId}`;
}

function friendlyGroupLabel(group: PendingUploadGroupState | undefined, groupId?: string): string {
  if (groupId && FRIENDLY_GROUP_LABELS[groupId]) {
    return FRIENDLY_GROUP_LABELS[groupId];
  }
  return group ? FRIENDLY_GROUP_LABELS[group.id] ?? group.label : "事故概况";
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${Math.round(bytes / 1024)} KB`;
  }
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / 1024 / 1024).toFixed(bytes >= 10 * 1024 * 1024 ? 0 : 1)} MB`;
  }
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function createLocalObjectUrl(file: File): string | null {
  if (typeof URL === "undefined" || typeof URL.createObjectURL !== "function") {
    return null;
  }
  try {
    return URL.createObjectURL(file);
  } catch {
    return null;
  }
}

function releaseLocalObjectUrl(url: string | null): void {
  if (url && typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") {
    URL.revokeObjectURL(url);
  }
}

function GroupGlyph({ groupId }: { groupId: string }) {
  if (groupId === "accident_videos") {
    return <Video aria-hidden size={16} />;
  }
  if (groupId === "scene") {
    return <MapPin aria-hidden size={16} />;
  }
  if (groupId === "privacy_photos") {
    return <LockKeyhole aria-hidden size={16} />;
  }
  if (groupId === "other_information") {
    return <FileText aria-hidden size={16} />;
  }
  return <ImageIcon aria-hidden size={16} />;
}

function MediaThumb({ item, url }: { item: PendingUploadItem; url: string | null }) {
  return (
    <div className="materials-thumb">
      {url && item.mediaType === "image" ? (
        <img src={url} alt="" loading="lazy" draggable={false} />
      ) : null}
      {url && item.mediaType === "video" ? (
        <video src={url} muted playsInline preload="metadata" aria-hidden />
      ) : null}
      {!url ? (
        <span className="materials-thumb-placeholder" aria-hidden>
          {item.mediaType === "video" ? <Film size={32} /> : <FileImage size={32} />}
        </span>
      ) : null}
      <span className="materials-type-badge">{item.mediaType === "video" ? "视频" : "图片"}</span>
    </div>
  );
}

export function MaterialsWorkspace({
  groups,
  limits,
  busy,
  onAdd,
  onDropFiles,
  onRemove,
  onGenerate,
}: MaterialsWorkspaceProps) {
  const [activeGroupId, setActiveGroupId] = useState(ALL_GROUP_ID);
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [isDragOver, setIsDragOver] = useState(false);
  const [selectedMaterial, setSelectedMaterial] = useState<SelectedMaterial | null>(null);
  const objectUrlsRef = useRef<Map<string, ObjectUrlEntry>>(new Map());
  const [, setObjectUrlVersion] = useState(0);
  const categoryNavRef = useRef<HTMLElement>(null);
  const dialogCloseButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const lastFocusedTriggerRef = useRef<HTMLButtonElement | null>(null);

  const orderedGroups = useMemo(
    () => groups.slice().sort((left, right) => left.sequence - right.sequence),
    [groups],
  );

  const allMaterials = useMemo<MaterialRecord[]>(
    () =>
      orderedGroups.flatMap((group) =>
        group.items.map((item) => ({
          key: materialKey(group.id, item.id),
          group,
          item,
        })),
      ),
    [orderedGroups],
  );

  const activeGroup = useMemo(
    () => orderedGroups.find((group) => group.id === activeGroupId),
    [activeGroupId, orderedGroups],
  );

  const visibleMaterials = useMemo(
    () =>
      activeGroupId === ALL_GROUP_ID
        ? allMaterials
        : allMaterials.filter((record) => record.group.id === activeGroupId),
    [activeGroupId, allMaterials],
  );

  const selectedRecord = useMemo(
    () =>
      selectedMaterial
        ? allMaterials.find(
            (record) => record.group.id === selectedMaterial.groupId && record.item.id === selectedMaterial.itemId,
          ) ?? null
        : null,
    [allMaterials, selectedMaterial],
  );

  const totalImages = allMaterials.filter((record) => record.item.mediaType === "image").length;
  const totalVideos = allMaterials.length - totalImages;
  const totalBytes = allMaterials.reduce((sum, record) => sum + record.item.sizeBytes, 0);
  const addGroupId = activeGroupId === ALL_GROUP_ID ? DEFAULT_ADD_GROUP_ID : activeGroupId;
  const addGroup = orderedGroups.find((group) => group.id === addGroupId);
  const addGroupLabel = friendlyGroupLabel(addGroup, addGroupId);
  const addGroupTitle = addGroup?.label ?? "事故参与方总体概况和损坏照片";
  const activeTitle = activeGroupId === ALL_GROUP_ID ? "全部资料" : friendlyGroupLabel(activeGroup);

  useEffect(() => {
    const liveKeys = new Set(allMaterials.map((record) => record.key));
    let changed = false;

    allMaterials.forEach((record) => {
      const existing = objectUrlsRef.current.get(record.key);
      if (existing?.file === record.item.file) {
        return;
      }
      if (existing) {
        releaseLocalObjectUrl(existing.url);
      }
      objectUrlsRef.current.set(record.key, {
        file: record.item.file,
        url: createLocalObjectUrl(record.item.file),
      });
      changed = true;
    });

    objectUrlsRef.current.forEach((entry, key) => {
      if (liveKeys.has(key)) {
        return;
      }
      releaseLocalObjectUrl(entry.url);
      objectUrlsRef.current.delete(key);
      changed = true;
    });

    if (changed) {
      setObjectUrlVersion((version) => version + 1);
    }
  }, [allMaterials]);

  useEffect(() => {
    return () => {
      objectUrlsRef.current.forEach((entry) => releaseLocalObjectUrl(entry.url));
      objectUrlsRef.current.clear();
    };
  }, []);

  useEffect(() => {
    if (activeGroupId !== ALL_GROUP_ID && !orderedGroups.some((group) => group.id === activeGroupId)) {
      setActiveGroupId(ALL_GROUP_ID);
    }
  }, [activeGroupId, orderedGroups]);

  useDialogFocus(dialogRef, Boolean(selectedRecord));

  useEffect(() => {
    if (selectedMaterial && !selectedRecord) {
      setSelectedMaterial(null);
    }
  }, [selectedMaterial, selectedRecord]);

  useEffect(() => {
    if (!selectedRecord) {
      const trigger = lastFocusedTriggerRef.current;
      if (trigger && document.contains(trigger)) {
        trigger.focus();
      }
      lastFocusedTriggerRef.current = null;
      return;
    }

    dialogCloseButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setSelectedMaterial(null);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [selectedRecord]);

  function handleAdd() {
    if (busy || !addGroup) {
      return;
    }
    onAdd(addGroup.id);
  }

  function handleDragOver(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (!busy && event.dataTransfer.types.includes("Files")) {
      event.dataTransfer.dropEffect = "copy";
      setIsDragOver(true);
    }
  }

  function handleDragLeave(event: DragEvent<HTMLDivElement>) {
    if (event.currentTarget.contains(event.relatedTarget as Node | null)) {
      return;
    }
    setIsDragOver(false);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setIsDragOver(false);
    if (busy || !addGroup) {
      return;
    }
    const files = Array.from(event.dataTransfer.files);
    if (files.length > 0) {
      onDropFiles(addGroup.id, files);
    }
  }

  function openMaterial(record: MaterialRecord, event: MouseEvent<HTMLButtonElement>) {
    lastFocusedTriggerRef.current = event.currentTarget;
    setSelectedMaterial({ groupId: record.group.id, itemId: record.item.id });
  }

  function scrollCategories() {
    categoryNavRef.current?.scrollBy({ left: 220, behavior: "smooth" });
  }

  const selectedUrl = selectedRecord
    ? objectUrlsRef.current.get(selectedRecord.key)?.url ?? null
    : null;

  return (
    <section className="materials-workspace" aria-label="事故资料工作区" aria-busy={busy}>
      <aside className="materials-category-rail">
        <div className="materials-rail-title">事故资料</div>
        <nav ref={categoryNavRef} className="materials-category-nav" aria-label="资料分类">
          <button
            type="button"
            className={`materials-category-button ${activeGroupId === ALL_GROUP_ID ? "is-active" : ""}`}
            aria-pressed={activeGroupId === ALL_GROUP_ID}
            onClick={() => setActiveGroupId(ALL_GROUP_ID)}
            title="全部资料"
          >
            <FileImage aria-hidden size={16} />
            <span>全部</span>
            {allMaterials.length > 0 && <b>{allMaterials.length}</b>}
          </button>
          {orderedGroups.map((group) => (
            <button
              type="button"
              key={group.id}
              className={`materials-category-button ${activeGroupId === group.id ? "is-active" : ""}`}
              aria-pressed={activeGroupId === group.id}
              onClick={() => setActiveGroupId(group.id)}
              title={group.label}
            >
              <GroupGlyph groupId={group.id} />
              <span>{friendlyGroupLabel(group)}</span>
              {group.items.length > 0 && <b>{group.items.length}</b>}
            </button>
          ))}
        </nav>
        <button type="button" className="materials-category-scroll" onClick={scrollCategories} aria-label="向右浏览资料分类">
          <ChevronRight aria-hidden size={16} />
        </button>
        <div className="materials-category-note">
          <LockKeyhole aria-hidden size={13} />
          <span>生成前，资料暂存在本机</span>
        </div>
      </aside>

      <div className="materials-workspace-main">
        <header className="materials-toolbar">
          <div className="materials-toolbar-heading">
            <div>
              <h2>{activeTitle}</h2>
              <p className="materials-add-destination" title={addGroupTitle}>
                添加归类：<strong>{addGroupLabel}</strong>
              </p>
            </div>
            {visibleMaterials.length > 0 && <span className="materials-toolbar-count">{visibleMaterials.length} 项</span>}
          </div>
          <div className="materials-toolbar-actions">
            <button
              type="button"
              className={`materials-icon-button ${viewMode === "grid" ? "is-pressed" : ""}`}
              aria-label="缩略图视图"
              title="缩略图视图"
              aria-pressed={viewMode === "grid"}
              onClick={() => setViewMode("grid")}
            >
              <Grid2X2 aria-hidden size={17} />
            </button>
            <button
              type="button"
              className={`materials-icon-button ${viewMode === "list" ? "is-pressed" : ""}`}
              aria-label="列表视图"
              title="列表视图"
              aria-pressed={viewMode === "list"}
              onClick={() => setViewMode("list")}
            >
              <List aria-hidden size={17} />
            </button>
            {visibleMaterials.length > 0 ? (
              <>
                <span className="materials-toolbar-divider" aria-hidden />
                <button
                  type="button"
                  className="materials-button materials-button-secondary materials-button-compact"
                  onClick={handleAdd}
                  disabled={busy || !addGroup}
                  title={`添加到${addGroupTitle}`}
                >
                  <Plus aria-hidden size={15} />
                  添加资料
                </button>
              </>
            ) : null}
          </div>
        </header>

        <div
          className="materials-surface"
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          {visibleMaterials.length === 0 ? (
            <div className="materials-empty-state">
              <div className="materials-empty-art" aria-hidden>
                <div className="materials-art-back"><FileImage size={27} /></div>
                <div className="materials-art-front"><ScanLine size={27} /></div>
                <span><Plus size={13} /></span>
              </div>
              <h2>{activeGroupId === ALL_GROUP_ID ? "从事故资料开始" : `${activeTitle}暂无资料`}</h2>
              <button
                type="button"
                className="materials-button materials-button-primary"
                onClick={handleAdd}
                disabled={busy || !addGroup}
                aria-label={`添加资料到${addGroupTitle}`}
              >
                <Plus aria-hidden size={16} />
                {activeGroupId === ALL_GROUP_ID ? "添加图片或视频" : `添加到${addGroupLabel}`}
              </button>
              <p>也可将文件拖到这里</p>
              <div className="materials-file-types" aria-label="支持图片和视频">
                <ImageIcon aria-hidden size={14} />
                <span>JPG / PNG</span>
                <i aria-hidden />
                <Film aria-hidden size={14} />
                <span>MP4</span>
              </div>
            </div>
          ) : (
            <div className={`materials-media-grid ${viewMode === "list" ? "is-list" : ""}`}>
              {visibleMaterials.map((record) => {
                const url = objectUrlsRef.current.get(record.key)?.url ?? null;
                const groupTitle = record.group.label;
                return (
                  <button
                    type="button"
                    className="materials-media-item"
                    key={record.key}
                    onClick={(event) => openMaterial(record, event)}
                    aria-label={`打开资料详情：${record.item.file.name}`}
                    title={record.item.file.name}
                  >
                    <MediaThumb item={record.item} url={url} />
                    <span className="materials-media-name" title={record.item.file.name}>{record.item.file.name}</span>
                    <span className="materials-media-meta">
                      <span>{formatBytes(record.item.sizeBytes)}</span>
                      <span className="materials-media-category" title={groupTitle}>{friendlyGroupLabel(record.group)}</span>
                    </span>
                  </button>
                );
              })}
            </div>
          )}
          {isDragOver ? (
            <div className="materials-drop-overlay" aria-live="polite">
              <Upload aria-hidden size={20} />
              松开以添加资料
            </div>
          ) : null}
        </div>

        <footer className="materials-workspace-footer">
          <div className="materials-footer-note" title={`图片单张不超过${formatBytes(limits.max_image_bytes)}，视频单个不超过${formatBytes(limits.max_video_bytes)}，总上传不超过${formatBytes(limits.max_total_bytes)}`}>
            <Info aria-hidden size={14} />
            <span>
              图片≤{formatBytes(limits.max_image_bytes)} · 视频≤{formatBytes(limits.max_video_bytes)} · 总量≤{formatBytes(limits.max_total_bytes)}
            </span>
          </div>
          <div className="materials-footer-action">
            <span className="materials-selection-summary" hidden={allMaterials.length === 0}>
              {totalImages} 张图片 · {totalVideos} 个视频 · {formatBytes(totalBytes)}
              <span className="materials-selection-limits">
                （上限 {limits.max_total_images} 张图片 / {limits.max_total_videos} 个视频）
              </span>
            </span>
            <button
              type="button"
              className="materials-button materials-button-primary materials-generate-button"
              onClick={onGenerate}
              disabled={busy || allMaterials.length === 0}
            >
              {busy ? "正在生成" : "生成事故事实"}
              <ArrowRight aria-hidden size={15} />
            </button>
          </div>
        </footer>
      </div>

      {selectedRecord ? (
        <div
          className="materials-overlay"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              setSelectedMaterial(null);
            }
          }}
        >
          <section ref={dialogRef} className="materials-dialog" role="dialog" aria-modal="true" aria-labelledby="materials-dialog-title">
            <header className="materials-dialog-header">
              <div>
                <h2 id="materials-dialog-title" title={selectedRecord.item.file.name}>{selectedRecord.item.file.name}</h2>
                <span>{selectedRecord.item.mediaType === "video" ? "视频" : "图片"} · {formatBytes(selectedRecord.item.sizeBytes)}</span>
              </div>
              <button
                ref={dialogCloseButtonRef}
                type="button"
                className="materials-icon-button"
                onClick={() => setSelectedMaterial(null)}
                aria-label="关闭资料详情"
                title="关闭资料详情"
              >
                <X aria-hidden size={18} />
              </button>
            </header>
            <div className="materials-dialog-media">
              {selectedUrl && selectedRecord.item.mediaType === "image" ? (
                <img src={selectedUrl} alt={selectedRecord.item.file.name} />
              ) : null}
              {selectedUrl && selectedRecord.item.mediaType === "video" ? (
                <video src={selectedUrl} controls playsInline preload="metadata" />
              ) : null}
              {!selectedUrl ? (
                <span className="materials-dialog-placeholder" aria-live="polite">
                  {selectedRecord.item.mediaType === "video" ? <Film size={58} /> : <FileImage size={58} />}
                  <span>当前环境无法读取本地预览</span>
                </span>
              ) : null}
            </div>
            <footer className="materials-dialog-footer">
              <div className="materials-dialog-category">
                <span>资料分类</span>
                <strong title={selectedRecord.group.label}>{friendlyGroupLabel(selectedRecord.group)}</strong>
              </div>
              <button
                type="button"
                className="materials-button materials-button-danger"
                disabled={busy}
                onClick={() => {
                  onRemove(selectedRecord.group.id, selectedRecord.item.id);
                  setSelectedMaterial(null);
                }}
              >
                <Trash2 aria-hidden size={15} />
                删除资料
              </button>
            </footer>
          </section>
        </div>
      ) : null}
    </section>
  );
}
