import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  createChatSession,
  deleteChatSession as deleteChatSessionApi,
  fetchChatSession as fetchChatSessionApi,
  formatApiErrorMessage,
  listChatSessions,
  sendChatSessionSnapshot,
  updateChatSession as updateChatSessionApi,
} from "./api";
import type {
  ChatMessage,
  ChatSessionApiRecord,
  ChatSessionLinkedArtifact,
  ChatSessionLinkedFile,
  ChatSessionUpsertPayload,
  GenerateInputFromUploadResponse,
  GenerateReportResponse,
} from "./types";

export interface ChatSession {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  sessionState?: "draft" | "input_ready" | "report_running" | "report_ready" | "export_ready" | "cancelled" | "failed";
  sortOrder?: number;
  sourceType?: "image" | "video" | "mixed";
  sourceName?: string;
  messages: ChatMessage[];
  draftJson: string;
  draftMeta?: GenerateInputFromUploadResponse | null;
  reportResult?: GenerateReportResponse | null;
  linkedFiles: ChatSessionLinkedFile[];
  linkedArtifacts: ChatSessionLinkedArtifact[];
}

type SessionMutableFields = Omit<ChatSession, "id" | "createdAt">;
type SessionUpdate =
  | Partial<SessionMutableFields>
  | ((session: ChatSession) => Partial<SessionMutableFields>);

const KNOWLEDGE_MESSAGE_PREFIX = "### 首轮知识库片段（节选）";
const AGENTIC_MESSAGE_PREFIX = "### Agentic RAG 新增片段（节选）";

const STORAGE_KEY = "traffic_accident_chat_sessions";
const SYNC_DEBOUNCE_MS = 300;

function sortSessions(sessions: ChatSession[]): ChatSession[] {
  return [...sessions].sort((left, right) => {
    const leftHasManualOrder = Number.isFinite(left.sortOrder);
    const rightHasManualOrder = Number.isFinite(right.sortOrder);

    if (leftHasManualOrder && rightHasManualOrder) {
      if (left.sortOrder !== right.sortOrder) {
        return (left.sortOrder ?? 0) - (right.sortOrder ?? 0);
      }
      return left.id.localeCompare(right.id);
    }

    if (leftHasManualOrder !== rightHasManualOrder) {
      return leftHasManualOrder ? -1 : 1;
    }

    if (left.createdAt !== right.createdAt) {
      return right.createdAt - left.createdAt;
    }
    return left.id.localeCompare(right.id);
  });
}

function normalizeKnowledgeMessageOrder(messages: ChatMessage[]): ChatMessage[] {
  const knowledgeMessages = messages.filter(
    (message) => message.kind === "markdown" && message.content.startsWith(KNOWLEDGE_MESSAGE_PREFIX),
  );
  const agenticMessages = messages.filter(
    (message) => message.kind === "markdown" && message.content.startsWith(AGENTIC_MESSAGE_PREFIX),
  );
  if (knowledgeMessages.length === 0 && agenticMessages.length === 0) {
    return messages;
  }

  const firstRelevantIndex = messages.findIndex(
    (message) =>
      message.kind === "markdown" &&
      (message.content.startsWith(KNOWLEDGE_MESSAGE_PREFIX) || message.content.startsWith(AGENTIC_MESSAGE_PREFIX)),
  );
  if (firstRelevantIndex < 0) {
    return messages;
  }

  const leading = messages.slice(0, firstRelevantIndex);
  const trailing = messages.slice(firstRelevantIndex).filter(
    (message) =>
      !(
        message.kind === "markdown" &&
        (message.content.startsWith(KNOWLEDGE_MESSAGE_PREFIX) || message.content.startsWith(AGENTIC_MESSAGE_PREFIX))
      ),
  );
  return [...leading, ...knowledgeMessages, ...agenticMessages, ...trailing];
}

function normalizeLinkedArtifacts(
  artifacts: ChatSessionLinkedArtifact[] | null | undefined,
): ChatSessionLinkedArtifact[] {
  if (!Array.isArray(artifacts)) {
    return [];
  }
  return artifacts.flatMap((artifact) => {
    if (!artifact || typeof artifact !== "object") {
      return [];
    }
    const category = typeof artifact.category === "string" ? artifact.category.trim() : "";
    const label = typeof artifact.label === "string" ? artifact.label.trim() : "";
    if (!category || !label) {
      return [];
    }
    return [
      {
        label,
        category,
        kind: typeof artifact.kind === "string" && artifact.kind.trim() ? artifact.kind.trim() : "collection",
        item_count: Number.isFinite(artifact.item_count) ? artifact.item_count : 0,
        summary: typeof artifact.summary === "string" ? artifact.summary : "",
      },
    ];
  });
}

function mapApiSession(record: ChatSessionApiRecord): ChatSession {
  return {
    id: record.id,
    title: record.title,
    createdAt: record.created_at,
    updatedAt: record.updated_at,
    sessionState: record.session_state ?? undefined,
    sortOrder: record.sort_order ?? undefined,
    sourceType: record.source_type ?? undefined,
    sourceName: record.source_name ?? undefined,
    messages: normalizeKnowledgeMessageOrder(record.messages ?? []),
    draftJson: record.draft_json ?? "",
    draftMeta: record.draft_meta ?? null,
    reportResult: record.report_result ?? null,
    linkedFiles: record.linked_files ?? [],
    linkedArtifacts: normalizeLinkedArtifacts(record.linked_artifacts),
  };
}

export function buildChatSessionPayload(session: ChatSession, includeId: boolean): ChatSessionUpsertPayload {
  return {
    ...(includeId ? { id: session.id, created_at: session.createdAt } : {}),
    title: session.title,
    updated_at: session.updatedAt,
    sort_order: session.sortOrder ?? null,
    source_type: session.sourceType ?? null,
    source_name: session.sourceName ?? null,
    messages: session.messages,
    draft_json: session.draftJson,
    draft_meta: session.draftMeta ?? null,
    report_result: session.reportResult ?? null,
  };
}

function normalizeLegacySession(rawSession: Partial<ChatSession> & { id?: string }): ChatSession | null {
  if (!rawSession.id) {
    return null;
  }
  return {
    id: rawSession.id,
    title: rawSession.title || "新交通事故",
    createdAt: rawSession.createdAt || Date.now(),
    updatedAt: rawSession.updatedAt || rawSession.createdAt || Date.now(),
    sortOrder: rawSession.sortOrder,
    sourceType: rawSession.sourceType,
    sourceName: rawSession.sourceName,
    messages: rawSession.messages || [],
    draftJson: rawSession.draftJson || "",
    draftMeta: rawSession.draftMeta ?? null,
    reportResult: rawSession.reportResult ?? null,
    linkedFiles: rawSession.linkedFiles || [],
    linkedArtifacts: normalizeLinkedArtifacts(rawSession.linkedArtifacts),
  };
}

function loadLegacySessions(): ChatSession[] {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (!stored) {
      return [];
    }
    const parsed = JSON.parse(stored) as Array<Partial<ChatSession> & { id?: string }>;
    return sortSessions(parsed.map(normalizeLegacySession).filter(Boolean) as ChatSession[]);
  } catch (error) {
    console.error("读取旧版本地会话失败", error);
    return [];
  }
}

async function migrateLegacySessions(): Promise<ChatSession[]> {
  const legacySessions = loadLegacySessions();
  if (legacySessions.length === 0) {
    return [];
  }

  const migrated: ChatSession[] = [];
  for (const session of legacySessions) {
    try {
      const created = await createChatSession(buildChatSessionPayload(session, true));
      migrated.push(mapApiSession(created));
    } catch (error) {
      console.error("迁移旧版本地会话失败", error);
      migrated.push(session);
    }
  }

  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch (error) {
    console.error("清理旧版本地会话失败", error);
  }
  return sortSessions(migrated);
}

export function useChatHistory() {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [isLoaded, setIsLoaded] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const syncTimersRef = useRef<Map<string, number>>(new Map());
  const mountedRef = useRef(true);
  const sessionsRef = useRef<ChatSession[]>([]);

  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);

  const replaceSession = useCallback((nextSession: ChatSession) => {
    const exists = sessionsRef.current.some((item) => item.id === nextSession.id);
    const nextSessions = sortSessions(
      exists
        ? sessionsRef.current.map((item) => (item.id === nextSession.id ? nextSession : item))
        : [nextSession, ...sessionsRef.current],
    );
    sessionsRef.current = nextSessions;
    setSessions(nextSessions);
  }, []);

  const persistSession = useCallback(
    async (session: ChatSession, forceCreate: boolean = false) => {
      try {
        const saved = forceCreate
          ? await createChatSession(buildChatSessionPayload(session, true))
          : await updateChatSessionApi(session.id, buildChatSessionPayload(session, false));
        if (!mountedRef.current) {
          return;
        }
        replaceSession(mapApiSession(saved));
        setSyncError(null);
      } catch (error) {
        if (!forceCreate && error instanceof ApiError && error.status === 404) {
          await persistSession(session, true);
          return;
        }
        if (!mountedRef.current) {
          return;
        }
        console.error("同步会话失败", error);
        setSyncError(formatApiErrorMessage(error, "同步会话失败。"));
      }
    },
    [replaceSession],
  );

  const scheduleSessionSync = useCallback(
    (session: ChatSession) => {
      const existingTimer = syncTimersRef.current.get(session.id);
      if (existingTimer) {
        window.clearTimeout(existingTimer);
      }
      const timerId = window.setTimeout(() => {
        syncTimersRef.current.delete(session.id);
        void persistSession(session);
      }, SYNC_DEBOUNCE_MS);
      syncTimersRef.current.set(session.id, timerId);
    },
    [persistSession],
  );

  const flushSessionById = useCallback(
    async (sessionId: string) => {
      const existingTimer = syncTimersRef.current.get(sessionId);
      if (existingTimer) {
        window.clearTimeout(existingTimer);
        syncTimersRef.current.delete(sessionId);
      }

      const session = sessionsRef.current.find((item) => item.id === sessionId);
      if (!session) {
        return;
      }
      await persistSession(session);
    },
    [persistSession],
  );

  const refreshSessionById = useCallback(
    async (sessionId: string) => {
      if (syncTimersRef.current.has(sessionId)) {
        await flushSessionById(sessionId);
      }

      try {
        const fetched = await fetchChatSessionApi(sessionId);
        if (!mountedRef.current) {
          return null;
        }
        const mapped = mapApiSession(fetched);
        replaceSession(mapped);
        setSyncError(null);
        return mapped;
      } catch (error) {
        if (!mountedRef.current) {
          return null;
        }

        if (error instanceof ApiError && error.status === 404) {
          const nextSessions = sessionsRef.current.filter((session) => session.id !== sessionId);
          sessionsRef.current = nextSessions;
          setSessions(nextSessions);
          setActiveSessionId((current) => (current === sessionId ? null : current));
          setSyncError("会话已不存在，已从列表移除。");
          return null;
        }

        console.error("刷新会话失败", error);
        setSyncError(formatApiErrorMessage(error, "刷新会话失败。"));
        return null;
      }
    },
    [flushSessionById, replaceSession],
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      for (const timerId of syncTimersRef.current.values()) {
        window.clearTimeout(timerId);
      }
      syncTimersRef.current.clear();
    };
  }, []);

  const persistSessionOnPagehide = useCallback((sessionId: string) => {
    const session = sessionsRef.current.find((item) => item.id === sessionId);
    if (!session) {
      return false;
    }
    return sendChatSessionSnapshot(buildChatSessionPayload(session, true));
  }, []);

  const persistPendingSessionsOnPagehide = useCallback((excludedSessionIds: string[] = []) => {
    const excludedIdSet = new Set(excludedSessionIds);
    for (const [sessionId, timerId] of syncTimersRef.current.entries()) {
      window.clearTimeout(timerId);
      if (!excludedIdSet.has(sessionId)) {
        persistSessionOnPagehide(sessionId);
      }
    }
    syncTimersRef.current.clear();
  }, [persistSessionOnPagehide]);

  useEffect(() => {
    let cancelled = false;

    async function loadSessions() {
      try {
        const remoteSessions = await listChatSessions();
        if (cancelled) {
          return;
        }
        if (remoteSessions.length > 0) {
          const nextSessions = sortSessions(remoteSessions.map(mapApiSession));
          sessionsRef.current = nextSessions;
          setSessions(nextSessions);
          setSyncError(null);
        } else {
          const migrated = await migrateLegacySessions();
          if (cancelled) {
            return;
          }
          sessionsRef.current = migrated;
          setSessions(migrated);
        }
      } catch (error) {
        if (cancelled) {
          return;
        }
        console.error("加载会话失败", error);
        setSyncError(formatApiErrorMessage(error, "加载会话失败。"));
        const legacySessions = loadLegacySessions();
        sessionsRef.current = legacySessions;
        setSessions(legacySessions);
      } finally {
        if (!cancelled) {
          setIsLoaded(true);
        }
      }
    }

    void loadSessions();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (sessions.length === 0) {
      if (activeSessionId) {
        setActiveSessionId(null);
      }
      return;
    }
    if (!activeSessionId || !sessions.some((session) => session.id === activeSessionId)) {
      setActiveSessionId(sessions[0].id);
    }
  }, [sessions, activeSessionId]);

  const activeSession = useMemo(
    () => sessions.find((session) => session.id === activeSessionId) || null,
    [sessions, activeSessionId],
  );

  const createNewSession = useCallback(
    (initialData?: Partial<ChatSession>) => {
      const newSession: ChatSession = {
        id: `session-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        title: "新交通事故",
        createdAt: Date.now(),
        updatedAt: Date.now(),
        sortOrder: sessionsRef.current.some((session) => Number.isFinite(session.sortOrder))
          ? Math.min(...sessionsRef.current
              .filter((session) => Number.isFinite(session.sortOrder))
              .map((session) => session.sortOrder as number)) - 1
          : undefined,
        sourceType: undefined,
        sourceName: undefined,
        messages: [],
        draftJson: "",
        draftMeta: null,
        reportResult: null,
        ...initialData,
        linkedFiles: initialData?.linkedFiles ?? [],
        linkedArtifacts: initialData?.linkedArtifacts ?? [],
      };
      const nextSessions = sortSessions([newSession, ...sessionsRef.current]);
      sessionsRef.current = nextSessions;
      setSessions(nextSessions);
      setActiveSessionId(newSession.id);
      void persistSession(newSession, true);
      return newSession;
    },
    [persistSession],
  );

  const updateSessionById = useCallback(
    (sessionId: string, updates: SessionUpdate) => {
      const currentSession = sessionsRef.current.find((session) => session.id === sessionId);
      if (!currentSession) {
        return;
      }

      const resolvedUpdates = typeof updates === "function" ? updates(currentSession) : updates;
      const nextSnapshot: ChatSession = {
        ...currentSession,
        ...resolvedUpdates,
        updatedAt: resolvedUpdates.updatedAt ?? Date.now(),
      };
      const nextSessions = sortSessions(
        sessionsRef.current.map((session) => (session.id === sessionId ? nextSnapshot : session)),
      );
      sessionsRef.current = nextSessions;
      setSessions(nextSessions);
      scheduleSessionSync(nextSnapshot);
    },
    [scheduleSessionSync],
  );

  const updateActiveSession = useCallback(
    (updates: SessionUpdate) => {
      if (!activeSessionId) {
        return;
      }
      updateSessionById(activeSessionId, updates);
    },
    [activeSessionId, updateSessionById],
  );

  const deleteSession = useCallback(
    async (sessionId: string) => {
      const existingTimer = syncTimersRef.current.get(sessionId);
      if (existingTimer) {
        window.clearTimeout(existingTimer);
        syncTimersRef.current.delete(sessionId);
      }
      try {
        await deleteChatSessionApi(sessionId);
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404)) {
          console.error("删除会话失败", error);
          throw error;
        }
      }
      const nextSessions = sessionsRef.current.filter((session) => session.id !== sessionId);
      sessionsRef.current = nextSessions;
      setSessions(nextSessions);
      setActiveSessionId((current) => (current === sessionId ? null : current));
      setSyncError(null);
    },
    [],
  );

  const reorderSessions = useCallback(
    async (orderedSessionIds: string[]) => {
      if (orderedSessionIds.length !== sessionsRef.current.length) {
        return;
      }

      const sessionMap = new Map(sessionsRef.current.map((session) => [session.id, session]));
      const nextSessions = orderedSessionIds.map((sessionId, index) => {
        const session = sessionMap.get(sessionId);
        if (!session) {
          throw new Error(`会话不存在，无法排序：${sessionId}`);
        }
        return {
          ...session,
          sortOrder: index,
        };
      });

      const sortedSessions = sortSessions(nextSessions);
      sessionsRef.current = sortedSessions;
      setSessions(sortedSessions);

      await Promise.all(sortedSessions.map((session) => persistSession(session)));
    },
    [persistSession],
  );

  return {
    sessions,
    activeSessionId,
    activeSession,
    isLoaded,
    syncError,
    setActiveSessionId,
    createNewSession,
    updateSessionById,
    flushSessionById,
    refreshSessionById,
    reorderSessions,
    updateActiveSession,
    persistSessionOnPagehide,
    persistPendingSessionsOnPagehide,
    deleteSession,
  };
}
