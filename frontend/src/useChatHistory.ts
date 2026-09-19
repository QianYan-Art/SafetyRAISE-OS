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
  UserSummary,
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

const SESSION_STORAGE_KEY_PREFIX = "traffic_accident_chat_sessions";
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

export function isServerSessionVersionFresh(
  fetchedUpdatedAt: number,
  knownUpdatedAt: number | undefined,
): boolean {
  return (
    !Number.isFinite(fetchedUpdatedAt)
    || knownUpdatedAt === undefined
    || !Number.isFinite(knownUpdatedAt)
    || fetchedUpdatedAt >= knownUpdatedAt
  );
}

function sessionValuesEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) {
    return true;
  }
  try {
    return JSON.stringify(left) === JSON.stringify(right);
  } catch {
    return false;
  }
}

function mergeConcurrentMessages(
  baseMessages: ChatMessage[],
  savedMessages: ChatMessage[],
  currentMessages: ChatMessage[],
): ChatMessage[] {
  const baseById = new Map(baseMessages.map((message) => [message.id, message]));
  const savedById = new Map(savedMessages.map((message) => [message.id, message]));
  const currentIds = new Set(currentMessages.map((message) => message.id));
  const merged = currentMessages.flatMap((message) => {
    const baseMessage = baseById.get(message.id);
    if (!baseMessage) {
      return [message];
    }
    const savedMessage = savedById.get(message.id);
    if (!savedMessage) {
      return sessionValuesEqual(message, baseMessage) ? [] : [message];
    }
    if (
      !sessionValuesEqual(savedMessage, baseMessage)
      && sessionValuesEqual(message, baseMessage)
    ) {
      return [savedMessage];
    }
    return [message];
  });

  for (const message of savedMessages) {
    if (!baseById.has(message.id) && !currentIds.has(message.id)) {
      merged.push(message);
    }
  }
  return merged;
}

export function mergeStrictSessionResult(
  baseSession: ChatSession,
  savedSession: ChatSession,
  currentSession: ChatSession,
  updates: Partial<SessionMutableFields>,
): ChatSession {
  const merged: ChatSession = { ...currentSession };
  for (const key of Object.keys(updates) as Array<keyof SessionMutableFields>) {
    if (key === "updatedAt") {
      continue;
    }
    if (key === "messages") {
      merged.messages = mergeConcurrentMessages(
        baseSession.messages,
        savedSession.messages,
        currentSession.messages,
      );
      continue;
    }
    if (sessionValuesEqual(currentSession[key], baseSession[key])) {
      Object.assign(merged, { [key]: savedSession[key] });
    }
  }
  return merged;
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

function buildSessionStorageKey(currentUser: Pick<UserSummary, "id">): string {
  return `${SESSION_STORAGE_KEY_PREFIX}:${currentUser.id}`;
}

function loadStoredSessions(storageKey: string): ChatSession[] {
  try {
    const stored = localStorage.getItem(storageKey);
    if (!stored) {
      return [];
    }
    const parsed = JSON.parse(stored) as Array<Partial<ChatSession> & { id?: string }>;
    return sortSessions(parsed.map(normalizeLegacySession).filter(Boolean) as ChatSession[]);
  } catch (error) {
    console.error("读取本地会话缓存失败", error);
    return [];
  }
}

function saveStoredSessions(storageKey: string, sessions: ChatSession[]): void {
  try {
    localStorage.setItem(storageKey, JSON.stringify(sessions));
  } catch (error) {
    console.error("写入本地会话缓存失败", error);
  }
}

export function useChatHistory(currentUser: Pick<UserSummary, "id">) {
  const storageKey = useMemo(
    () => buildSessionStorageKey(currentUser),
    [currentUser.id],
  );
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [isLoaded, setIsLoaded] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const syncTimersRef = useRef<Map<string, number>>(new Map());
  const serverVersionsRef = useRef<Map<string, number>>(new Map());
  const persistenceQueueRef = useRef<Map<string, Promise<unknown>>>(new Map());
  const persistenceErrorsRef = useRef<Map<string, unknown>>(new Map());
  const saveTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const sessionsRef = useRef<ChatSession[]>([]);

  const rememberServerSession = useCallback((record: ChatSessionApiRecord): ChatSession => {
    if (Number.isFinite(record.updated_at)) {
      const previousVersion = serverVersionsRef.current.get(record.id);
      if (previousVersion === undefined || record.updated_at >= previousVersion) {
        serverVersionsRef.current.set(record.id, record.updated_at);
      }
    }
    return mapApiSession(record);
  }, []);

  const resolveFreshServerSession = useCallback(
    (record: ChatSessionApiRecord): ChatSession | null => {
      const knownUpdatedAt = serverVersionsRef.current.get(record.id);
      if (!isServerSessionVersionFresh(record.updated_at, knownUpdatedAt)) {
        return sessionsRef.current.find((item) => item.id === record.id) ?? null;
      }
      return rememberServerSession(record);
    },
    [rememberServerSession],
  );

  const enqueueSessionPersistence = useCallback(
    <T,>(sessionId: string, operation: () => Promise<T>): Promise<T> => {
      const previous = persistenceQueueRef.current.get(sessionId) ?? Promise.resolve();
      const next = previous.catch(() => undefined).then(operation);
      let tracked: Promise<T>;
      tracked = next.finally(() => {
        if (persistenceQueueRef.current.get(sessionId) === tracked) {
          persistenceQueueRef.current.delete(sessionId);
        }
      });
      persistenceQueueRef.current.set(sessionId, tracked);
      return tracked;
    },
    [],
  );

  const waitForSessionPersistence = useCallback(async (sessionId: string): Promise<void> => {
    const inFlight = persistenceQueueRef.current.get(sessionId);
    if (inFlight) {
      await inFlight.catch((error) => {
        persistenceErrorsRef.current.set(sessionId, error);
      });
    }
  }, []);

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

  const reportSyncFailure = useCallback((error: unknown, fallback: string) => {
    if (!mountedRef.current) {
      return;
    }
    console.error("同步会话失败", error);
    setSyncError(formatApiErrorMessage(error, fallback));
  }, []);

  const clearRecoveredSyncError = useCallback(() => {
    if (persistenceErrorsRef.current.size === 0) {
      setSyncError(null);
    }
  }, []);

  const persistSession = useCallback(
    async (session: ChatSession, forceCreate: boolean = false): Promise<ChatSession> => {
      const operation = enqueueSessionPersistence(session.id, async () => {
        const sessionToPersist = sessionsRef.current.find((item) => item.id === session.id) ?? session;
        let saved: ChatSessionApiRecord;
        if (forceCreate) {
          saved = await createChatSession(buildChatSessionPayload(sessionToPersist, true));
        } else {
          let expectedUpdatedAt = serverVersionsRef.current.get(session.id);
          if (!Number.isFinite(expectedUpdatedAt)) {
            const fetched = await fetchChatSessionApi(session.id);
            rememberServerSession(fetched);
            expectedUpdatedAt = serverVersionsRef.current.get(session.id);
          }
          if (!Number.isFinite(expectedUpdatedAt)) {
            throw new Error("缺少服务端会话版本，已拒绝严格保存。");
          }

          try {
            saved = await updateChatSessionApi(
              sessionToPersist.id,
              {
                ...buildChatSessionPayload(sessionToPersist, false),
                expected_updated_at: expectedUpdatedAt,
              },
            );
          } catch (error) {
            if (!forceCreate && error instanceof ApiError && error.status === 404) {
              saved = await createChatSession(buildChatSessionPayload(sessionToPersist, true));
            } else {
              throw error;
            }
          }
        }

        const mapped = rememberServerSession(saved);
        persistenceErrorsRef.current.delete(session.id);
        if (!mountedRef.current) {
          return mapped;
        }
        const currentSession = sessionsRef.current.find((item) => item.id === session.id);
        if (currentSession === sessionToPersist) {
          replaceSession(mapped);
        }
        clearRecoveredSyncError();
        return mapped;
      });

      return operation.catch((error) => {
        persistenceErrorsRef.current.set(session.id, error);
        throw error;
      });
    },
    [clearRecoveredSyncError, enqueueSessionPersistence, rememberServerSession, replaceSession],
  );

  const scheduleSessionSync = useCallback(
    (session: ChatSession) => {
      const existingTimer = syncTimersRef.current.get(session.id);
      if (existingTimer) {
        window.clearTimeout(existingTimer);
      }
      const timerId = window.setTimeout(() => {
        syncTimersRef.current.delete(session.id);
        void persistSession(session).catch((error) => {
          reportSyncFailure(error, "同步会话失败。");
        });
      }, SYNC_DEBOUNCE_MS);
      syncTimersRef.current.set(session.id, timerId);
    },
    [persistSession, reportSyncFailure],
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
      try {
        await persistSession(session);
      } catch (error) {
        reportSyncFailure(error, "保存会话失败。");
        throw error;
      }
    },
    [persistSession, reportSyncFailure],
  );

  const saveSessionById = useCallback(
    async (sessionId: string, updates?: SessionUpdate): Promise<ChatSession> => {
      const existingTimer = syncTimersRef.current.get(sessionId);
      if (existingTimer) {
        window.clearTimeout(existingTimer);
        syncTimersRef.current.delete(sessionId);
      }

      return enqueueSessionPersistence(sessionId, async () => {
        try {
          const session = sessionsRef.current.find((item) => item.id === sessionId);
          if (!session) {
            throw new Error("会话不存在，无法保存。");
          }

          const resolvedUpdates = updates === undefined
            ? {}
            : typeof updates === "function"
              ? updates(session)
              : updates;
          const nextSession: ChatSession = {
            ...session,
            ...resolvedUpdates,
            updatedAt: resolvedUpdates.updatedAt ?? Date.now(),
          };

          let expectedUpdatedAt = serverVersionsRef.current.get(sessionId);
          if (!Number.isFinite(expectedUpdatedAt)) {
            const fetched = await fetchChatSessionApi(sessionId);
            rememberServerSession(fetched);
            expectedUpdatedAt = serverVersionsRef.current.get(sessionId);
          }
          if (!Number.isFinite(expectedUpdatedAt)) {
            throw new Error("缺少服务端会话版本，已拒绝严格保存。");
          }

          const saved = await updateChatSessionApi(
            nextSession.id,
            {
              ...buildChatSessionPayload(nextSession, false),
              expected_updated_at: expectedUpdatedAt,
            },
          );
          const mapped = rememberServerSession(saved);
          persistenceErrorsRef.current.delete(sessionId);
          if (mountedRef.current) {
            const currentSession = sessionsRef.current.find((item) => item.id === sessionId);
            if (currentSession === session) {
              replaceSession(mapped);
            } else if (currentSession) {
              replaceSession(mergeStrictSessionResult(session, mapped, currentSession, resolvedUpdates));
            }
            clearRecoveredSyncError();
          }
          return mapped;
        } catch (error) {
          persistenceErrorsRef.current.set(sessionId, error);
          reportSyncFailure(error, "保存会话失败。");
          throw error;
        }
      });
    },
    [clearRecoveredSyncError, enqueueSessionPersistence, rememberServerSession, replaceSession, reportSyncFailure],
  );

  const refreshSessionById = useCallback(
    async (sessionId: string) => {
      try {
        if (syncTimersRef.current.has(sessionId)) {
          await flushSessionById(sessionId);
        }
        await waitForSessionPersistence(sessionId);
        const persistenceError = persistenceErrorsRef.current.get(sessionId);
        if (persistenceError) {
          reportSyncFailure(persistenceError, "保存会话失败，已保留本地未保存内容。");
          return null;
        }

        const fetched = await fetchChatSessionApi(sessionId);
        const mapped = resolveFreshServerSession(fetched);
        if (!mountedRef.current) {
          return null;
        }
        if (!mapped) {
          return null;
        }
        replaceSession(mapped);
        clearRecoveredSyncError();
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

        if (persistenceErrorsRef.current.has(sessionId)) {
          reportSyncFailure(error, "保存会话失败，已保留本地未保存内容。");
          return null;
        }
        reportSyncFailure(error, "刷新会话失败。");
        return null;
      }
    },
    [clearRecoveredSyncError, flushSessionById, replaceSession, reportSyncFailure, resolveFreshServerSession, waitForSessionPersistence],
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (saveTimerRef.current !== null) {
        window.clearTimeout(saveTimerRef.current);
        saveStoredSessions(storageKey, sessionsRef.current);
        saveTimerRef.current = null;
      }
      for (const timerId of syncTimersRef.current.values()) {
        window.clearTimeout(timerId);
      }
      syncTimersRef.current.clear();
    };
  }, [storageKey]);

  useEffect(() => {
    if (!isLoaded) {
      return;
    }
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current);
    }
    saveTimerRef.current = window.setTimeout(() => {
      saveStoredSessions(storageKey, sessions);
      saveTimerRef.current = null;
    }, SYNC_DEBOUNCE_MS);
    return () => {
      if (saveTimerRef.current !== null) {
        window.clearTimeout(saveTimerRef.current);
        saveTimerRef.current = null;
      }
    };
  }, [isLoaded, sessions, storageKey]);

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
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current);
      saveStoredSessions(storageKey, sessionsRef.current);
      saveTimerRef.current = null;
    }
    setSessions([]);
    sessionsRef.current = [];
    setActiveSessionId(null);
    setIsLoaded(false);
    setSyncError(null);
    serverVersionsRef.current.clear();
    persistenceErrorsRef.current.clear();
    for (const timerId of syncTimersRef.current.values()) {
      window.clearTimeout(timerId);
    }
    syncTimersRef.current.clear();

    async function loadSessions() {
      try {
        const remoteSessions = await listChatSessions();
        if (cancelled) {
          return;
        }
        if (remoteSessions.length > 0) {
          const nextSessions = sortSessions(remoteSessions.map(rememberServerSession));
          sessionsRef.current = nextSessions;
          setSessions(nextSessions);
          setSyncError(null);
        } else {
          const storedSessions = loadStoredSessions(storageKey);
          sessionsRef.current = storedSessions;
          setSessions(storedSessions);
        }
      } catch (error) {
        if (cancelled) {
          return;
        }
        console.error("加载会话失败", error);
        setSyncError(formatApiErrorMessage(error, "加载会话失败。"));
        const storedSessions = loadStoredSessions(storageKey);
        sessionsRef.current = storedSessions;
        setSessions(storedSessions);
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
  }, [rememberServerSession, storageKey]);

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
      void persistSession(newSession, true).catch((error) => {
        reportSyncFailure(error, "新建会话同步失败。");
      });
      return newSession;
    },
    [persistSession, reportSyncFailure],
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
      await waitForSessionPersistence(sessionId);
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
    [waitForSessionPersistence],
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

      try {
        await Promise.all(sortedSessions.map((session) => persistSession(session)));
      } catch (error) {
        reportSyncFailure(error, "会话排序同步失败。");
      }
     },
    [persistSession, reportSyncFailure],
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
    saveSessionById,
    refreshSessionById,
    reorderSessions,
    updateActiveSession,
    persistSessionOnPagehide,
    persistPendingSessionsOnPagehide,
    deleteSession,
  };
}
