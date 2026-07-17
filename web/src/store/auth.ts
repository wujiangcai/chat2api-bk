"use client";

import localforage from "localforage";

export type AuthRole = "admin" | "user";

export type StoredAuthSession = {
  key?: string;
  sessionMode?: "token" | "cookie";
  role: AuthRole;
  subjectId: string;
  keyId?: string;
  email?: string;
  name: string;
  quotaBalance?: number | null;
  packageName?: string | null;
};

export const AUTH_KEY_STORAGE_KEY = "chatgpt2api_auth_key";
export const AUTH_SESSION_STORAGE_KEY = "chatgpt2api_auth_session";

const authStorage = localforage.createInstance({
  name: "chatgpt2api",
  storeName: "auth",
});

function normalizeSession(value: unknown, fallbackKey = ""): StoredAuthSession | null {
  if (!value || typeof value !== "object") {
    return null;
  }

  const candidate = value as Partial<StoredAuthSession>;
  const key = String(candidate.key || fallbackKey || "").trim();
  const sessionMode = candidate.sessionMode === "cookie" ? "cookie" : "token";
  const role = candidate.role === "admin" || candidate.role === "user" ? candidate.role : null;
  if ((!key && sessionMode !== "cookie") || !role) {
    return null;
  }

  return {
    key,
    sessionMode,
    role,
    subjectId: String(candidate.subjectId || "").trim(),
    keyId: String(candidate.keyId || "").trim() || undefined,
    email: String(candidate.email || "").trim() || undefined,
    name: String(candidate.name || "").trim(),
    quotaBalance: typeof candidate.quotaBalance === "number" ? candidate.quotaBalance : null,
    packageName: typeof candidate.packageName === "string" ? candidate.packageName : null,
  };
}

export function getDefaultRouteForRole(role: AuthRole) {
  return role === "admin" ? "/accounts" : "/image";
}

export async function getStoredAuthKey() {
  if (typeof window === "undefined") {
    return "";
  }
  const storedSession = await authStorage.getItem<StoredAuthSession>(AUTH_SESSION_STORAGE_KEY);
  if (storedSession?.sessionMode === "cookie") {
    return "";
  }
  const value = await authStorage.getItem<string>(AUTH_KEY_STORAGE_KEY);
  return String(value || "").trim();
}

export async function getStoredAuthSession() {
  if (typeof window === "undefined") {
    return null;
  }

  const [storedKey, storedSession] = await Promise.all([
    authStorage.getItem<string>(AUTH_KEY_STORAGE_KEY),
    authStorage.getItem<StoredAuthSession>(AUTH_SESSION_STORAGE_KEY),
  ]);

  const normalizedSession = normalizeSession(storedSession, String(storedKey || ""));
  if (normalizedSession) {
    if (normalizedSession.sessionMode === "cookie") {
      await authStorage.removeItem(AUTH_KEY_STORAGE_KEY);
    } else if (normalizedSession.key !== String(storedKey || "").trim()) {
      await authStorage.setItem(AUTH_KEY_STORAGE_KEY, normalizedSession.key);
    }
    return normalizedSession;
  }

  if (String(storedKey || "").trim()) {
    await clearStoredAuthSession();
  }
  return null;
}

export async function setStoredAuthSession(session: StoredAuthSession) {
  const normalizedSession = normalizeSession(session);
  if (!normalizedSession) {
    await clearStoredAuthSession();
    return;
  }

  if (normalizedSession.sessionMode === "cookie") {
    await Promise.all([
      authStorage.removeItem(AUTH_KEY_STORAGE_KEY),
      authStorage.setItem(AUTH_SESSION_STORAGE_KEY, normalizedSession),
    ]);
    return;
  }

  await Promise.all([
    authStorage.setItem(AUTH_KEY_STORAGE_KEY, normalizedSession.key || ""),
    authStorage.setItem(AUTH_SESSION_STORAGE_KEY, normalizedSession),
  ]);
}

export async function setStoredAuthKey(authKey: string) {
  const normalizedAuthKey = String(authKey || "").trim();
  if (!normalizedAuthKey) {
    await clearStoredAuthSession();
    return;
  }
  await authStorage.setItem(AUTH_KEY_STORAGE_KEY, normalizedAuthKey);
}

export async function clearStoredAuthSession() {
  if (typeof window === "undefined") {
    return;
  }
  await Promise.all([
    authStorage.removeItem(AUTH_KEY_STORAGE_KEY),
    authStorage.removeItem(AUTH_SESSION_STORAGE_KEY),
  ]);
}

export async function clearStoredAuthKey() {
  await clearStoredAuthSession();
}
