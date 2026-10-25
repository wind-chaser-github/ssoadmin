import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import axios from 'axios';
import type { AxiosInstance } from 'axios';

type AppQuota = {
  app_id: string;
  quota: number;
  daily_quota: number;
  daily_quota_override: number | null;
  default_daily_quota: number;
  quota_reset_date: string | null;
  quota_managed: boolean;
};

type QuotaUser = {
  id: number;
  username: string;
  role: string;
  is_admin: boolean;
  created_at: string;
  apps: AppQuota[];
  admin_label: string;
  learner_names: string[];
  account_profile?: { nickname: string; avatar: string };
};

const userName = (user: QuotaUser) => user.admin_label || user.account_profile?.nickname || user.learner_names?.join('、') || (user.username.startsWith('wx_') ? '未填写昵称' : user.username);

function UserLabel({ user, disabled, onSave }: { user: QuotaUser; disabled: boolean; onSave: (user: QuotaUser, label: string) => Promise<void> }) {
  const [label, setLabel] = useState(user.admin_label || '');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return <details className="text-sm text-neutral-400">
    <summary className="cursor-pointer">编辑管理备注</summary>
    <form className="mt-3 flex flex-wrap gap-2" onSubmit={async event => {
      event.preventDefault(); if (busy || disabled) return;
      setBusy(true); setError('');
      try { await onSave(user, label); } catch (failure) { setError(errorMessage(failure)); }
      finally { setBusy(false); }
    }}>
      <input aria-label={`账号${user.id}的管理备注`} maxLength={48} value={label} onChange={event => setLabel(event.target.value)} disabled={busy || disabled} placeholder="例如：小明妈妈（仅管理员可见）" className="rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-white" />
      <button disabled={busy || disabled} className="rounded-lg border border-neutral-700 px-3 py-2 disabled:opacity-40">{busy ? '保存中…' : '保存备注'}</button>
      {error && <p role="alert" className="w-full text-red-300">{error}</p>}
    </form>
  </details>;
}

type QuotaEdit = { user: QuotaUser; app: AppQuota; kind: 'daily' | 'remaining' };

function errorMessage(error: unknown) {
  if (axios.isAxiosError(error)) {
    if (error.response?.status === 401) return '登录已失效，请重新登录后重试。';
    if (error.response?.status === 403) return '当前账号没有管理额度的权限。';
    const detail: unknown = error.response?.data?.detail;
    if (typeof detail === 'string') return detail;
  }
  return '请求失败，请检查网络后重试。';
}

function QuotaEditor({ edit, onClose, onSave }: {
  edit: QuotaEdit;
  onClose: () => void;
  onSave: (edit: QuotaEdit, value: number | null) => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const submitting = useRef(false);
  const daily = edit.kind === 'daily';
  const [value, setValue] = useState(String(daily ? edit.app.daily_quota : edit.app.quota));
  const [useDefault, setUseDefault] = useState(daily && edit.app.daily_quota_override === null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting.current) return;
    const parsed = Number(value);
    if (!(daily && useDefault) && (!/^\d+$/.test(value) || !Number.isSafeInteger(parsed) || parsed > 100000)) {
      setError('请输入 0–100000 之间的整数，不支持小数、负数或空值。');
      return;
    }
    submitting.current = true;
    setSaving(true);
    setError('');
    try {
      await onSave(edit, daily && useDefault ? null : parsed);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      submitting.current = false;
      setSaving(false);
    }
  };

  return (
    <dialog
      ref={dialog}
      aria-labelledby="quota-editor-title"
      aria-describedby="quota-editor-description"
      onCancel={event => { event.preventDefault(); if (!saving) onClose(); }}
      className="m-auto w-[calc(100%-2rem)] max-w-lg rounded-2xl border border-neutral-700 bg-neutral-900 p-0 text-neutral-300 shadow-2xl backdrop:bg-black/70"
    >
      <form onSubmit={handleSubmit} className="space-y-5 p-6">
        <div>
          <h2 id="quota-editor-title" className="text-xl font-bold text-white">{daily ? '修改每日额度' : '设置今日剩余'}</h2>
          <p className="mt-2 break-all text-sm text-neutral-400">{userName(edit.user)} · 账号 #{edit.user.id} · {edit.app.app_id}</p>
        </div>
        <p id="quota-editor-description" className="rounded-lg border border-indigo-500/20 bg-indigo-500/10 p-3 text-sm leading-6 text-indigo-200">
          {daily
            ? '每日额度是长期配置，下次 UTC 日切（北京时间 08:00）起生效，今日剩余保持不变。'
            : '直接将今日剩余设为输入的数值，并非在原有余额上增加；立即生效，下次日切按每日额度恢复。'}
        </p>
        {daily && (
          <fieldset disabled={saving} className="space-y-3 text-sm">
            <legend className="mb-2 font-medium text-white">额度来源</legend>
            <label className="flex items-center gap-2">
              <input type="radio" name="quota-source" checked={!useDefault} onChange={() => setUseDefault(false)} />
              指定每日额度
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" name="quota-source" checked={useDefault} onChange={() => setUseDefault(true)} />
              恢复应用默认（每日 {edit.app.default_daily_quota}）
            </label>
          </fieldset>
        )}
        <div>
          <label htmlFor="quota-editor-value" className="mb-2 block text-sm font-medium text-white">{daily ? '每日额度' : '今日剩余'}</label>
          <input
            id="quota-editor-value"
            type="text"
            inputMode="numeric"
            value={daily && useDefault ? String(edit.app.default_daily_quota) : value}
            onChange={event => setValue(event.target.value)}
            disabled={saving || (daily && useDefault)}
            aria-describedby="quota-value-help"
            className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2.5 text-white outline-none focus:border-indigo-400 disabled:opacity-50"
          />
          <p id="quota-value-help" className="mt-2 text-xs text-neutral-400">范围 0–100000；0 为有效额度。</p>
        </div>
        {error && <p role="alert" className="rounded-lg bg-red-500/10 p-3 text-sm text-red-300">{error}</p>}
        <div className="flex justify-end gap-3">
          <button type="button" onClick={onClose} disabled={saving} className="rounded-lg border border-neutral-700 px-4 py-2 text-sm hover:bg-neutral-800 disabled:opacity-50">取消</button>
          <button type="submit" disabled={saving} className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50">{saving ? '保存中…' : daily && useDefault ? '恢复默认' : '保存设置'}</button>
        </div>
      </form>
    </dialog>
  );
}

export default function UsersView({ api }: { api: AxiosInstance }) {
  const [users, setUsers] = useState<QuotaUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [notice, setNotice] = useState('');
  const [editor, setEditor] = useState<QuotaEdit | null>(null);
  const [query, setQuery] = useState('');
  const requestId = useRef(0);
  const mutationPending = useRef(false);

  const loadUsers = useCallback(async (afterSave = false, signal?: AbortSignal) => {
    const currentRequest = ++requestId.current;
    setLoading(true);
    setLoadError('');
    try {
      const response = await api.get<QuotaUser[]>('/admin/quotas/users', { signal });
      if (currentRequest !== requestId.current || signal?.aborted) return;
      setUsers(response.data);
      if (afterSave) setNotice('额度设置已保存，已重新读取服务器最新余额。');
    } catch (failure) {
      if (currentRequest !== requestId.current || signal?.aborted) return;
      setLoadError(`${afterSave ? '额度已保存，但列表刷新失败；请重试以查看最新余额。' : ''}${errorMessage(failure)}`);
    } finally {
      if (currentRequest === requestId.current && !signal?.aborted) setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    const controller = new AbortController();
    void loadUsers(false, controller.signal);
    return () => controller.abort();
  }, [loadUsers]);

  const saveQuota = async (edit: QuotaEdit, value: number | null) => {
    if (mutationPending.current) throw new Error('正在保存，请稍候。');
    mutationPending.current = true;
    setSaving(true);
    setNotice('');
    try {
      // Long-term policy and today's absolute balance use separate writes; never infer one from the other.
      const path = `/admin/quotas/users/${edit.user.id}/apps/${encodeURIComponent(edit.app.app_id)}/${edit.kind}`;
      await api.post(path, edit.kind === 'daily' ? { daily_quota: value } : { quota: value });
      setEditor(null);
      // The server settles UTC rollover and concurrent spending, so always refresh authoritative balances.
      await loadUsers(true);
    } finally {
      mutationPending.current = false;
      setSaving(false);
    }
  };

  const editDisabled = loading || saving || Boolean(loadError);
  const saveLabel = async (user: QuotaUser, label: string) => {
    if (mutationPending.current) throw new Error('正在保存，请稍候。');
    mutationPending.current = true; setSaving(true); setNotice('');
    try {
      await api.post(`/admin/quotas/users/${user.id}/label`, { label });
      await loadUsers();
      setNotice('管理备注已保存。备注仅供管理员识别账号，不修改用户登录名。');
    } finally { mutationPending.current = false; setSaving(false); }
  };
  const needle = query.trim().toLocaleLowerCase();
  const visibleUsers = users.filter(user => [String(user.id), `#${user.id}`, user.username, user.admin_label, user.account_profile?.nickname, ...(user.learner_names || [])].join(' ').toLocaleLowerCase().includes(needle));
  const editButtonClass = 'rounded-lg border border-neutral-700 px-3 py-1.5 text-xs text-neutral-200 transition-colors hover:border-indigo-400 hover:text-white disabled:cursor-not-allowed disabled:opacity-40';

  return (
    <section className="space-y-6" aria-busy={loading || saving}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white">用户与额度</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-neutral-400">每日额度为长期配置；今日剩余为当前可用余额。每日在 UTC 00:00（北京时间 08:00）恢复额度，修改每日配置从下次日切生效。</p>
        </div>
        <button type="button" disabled={loading || saving || Boolean(editor)} onClick={() => { setNotice(''); void loadUsers(); }} className={editButtonClass}>{loading ? '加载中…' : '刷新列表'}</button>
      </div>
      {notice && <p role="status" className="rounded-xl border border-emerald-500/20 bg-emerald-500/10 p-4 text-sm text-emerald-300">{notice}</p>}
      {loadError && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-red-500/20 bg-red-500/10 p-4 text-sm text-red-300">
          <p>{loadError}</p>
          <button type="button" onClick={() => { setNotice(''); void loadUsers(); }} disabled={loading || saving} className={editButtonClass}>重试加载</button>
        </div>
      )}
      {loading && users.length === 0 && <p role="status" className="text-sm text-neutral-400">正在读取用户和额度…</p>}
      <div className="space-y-2">
        <label htmlFor="user-search" className="block text-sm text-neutral-300">查找用户</label>
        <input id="user-search" type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="输入账号昵称、孩子小名、管理备注或账号编号" className="w-full rounded-xl border border-neutral-700 bg-neutral-900 px-4 py-3 text-white" />
        <p className="text-xs text-neutral-500">显示 {visibleUsers.length} / {users.length} 个账号。账号昵称和孩子档案分别显示；同名用户请核对账号编号，额度按登录账号分别计算。</p>
      </div>
      {!loading && !loadError && users.length === 0 && <p className="rounded-xl border border-neutral-800 p-6 text-neutral-400">暂无用户。</p>}
      {!loading && !loadError && users.length > 0 && visibleUsers.length === 0 && <p className="rounded-xl border border-neutral-800 p-6 text-neutral-400">没有找到匹配账号，请换一个昵称或账号编号。</p>}
      <div className="space-y-5">
        {visibleUsers.map(user => (
          <article key={user.id} className="overflow-hidden rounded-2xl border border-neutral-800 bg-neutral-900">
            <header className="flex flex-wrap items-center justify-between gap-3 border-b border-neutral-800 px-5 py-4">
              <div className="flex min-w-0 flex-wrap items-center gap-3">
                <div className="flex items-center gap-3">
                  {user.account_profile?.avatar && <img className="h-11 w-11 shrink-0 rounded-full object-cover" src={user.account_profile.avatar} alt="" />}
                  <h2 className="break-all font-semibold text-white">{userName(user)}</h2>
                </div>
                <span className="text-sm text-indigo-300">账号 #{user.id}</span>
                <span className={`rounded-md px-2 py-1 text-xs ${user.is_admin ? 'bg-indigo-500/20 text-indigo-300' : 'bg-neutral-800 text-neutral-300'}`}>{user.is_admin ? '管理员' : user.role}</span>
              </div>
              <p className="text-xs text-neutral-500">注册于 {new Date(user.created_at).toLocaleDateString('zh-CN')}</p>
            </header>
            <div className="space-y-2 border-b border-neutral-800 px-5 py-3">
              {user.account_profile?.nickname && <p className="text-sm text-neutral-300">账号昵称：{user.account_profile.nickname}</p>}
              {user.username.startsWith('wx_') && !user.account_profile?.nickname && <p className="text-sm text-amber-200/80">尚未填写账号昵称，请用户在小程序首页或“我的”中设置昵称与头像。</p>}
              <details className="text-xs text-neutral-500"><summary className="cursor-pointer">登录标识</summary><p className="mt-2 break-all">{user.username}</p></details>
              {user.learner_names?.length > 0 && <p className="text-sm text-neutral-300">英语学习档案：{user.learner_names.join('、')}</p>}
              <UserLabel key={`${user.id}:${user.admin_label}`} user={user} disabled={editDisabled} onSave={saveLabel} />
            </div>
            <div className="grid gap-4 p-5 xl:grid-cols-2">
              {user.apps.map(app => {
                const managed = app.quota_managed;
                return (
                  <div key={app.app_id} className="rounded-xl border border-neutral-800 bg-neutral-950 p-4">
                    <h3 className="break-all text-sm font-medium text-white">{app.app_id === 'seth-english' ? '小C英语 · seth-english' : app.app_id}</h3>
                    {managed ? (
                      <>
                        <dl className="mt-4 grid grid-cols-2 gap-4">
                          <div>
                            <dt className="text-xs text-neutral-400">每日额度</dt>
                            <dd className="mt-1 text-2xl font-semibold text-white">{app.daily_quota}</dd>
                            <dd className="mt-1 text-xs text-neutral-500">{app.daily_quota_override === null ? '使用应用默认' : `单独配置 · 应用默认 ${app.default_daily_quota}`}</dd>
                          </div>
                          <div>
                            <dt className="text-xs text-neutral-400">今日剩余</dt>
                            <dd className="mt-1 text-2xl font-semibold text-indigo-300">{app.quota}</dd>
                            <dd className="mt-1 text-xs text-neutral-500">当前可用余额</dd>
                          </div>
                        </dl>
                        <div className="mt-4 flex flex-wrap gap-2 border-t border-neutral-800 pt-4">
                          <button type="button" disabled={editDisabled} onClick={() => setEditor({ user, app, kind: 'daily' })} className={editButtonClass}>修改每日额度 / 恢复默认</button>
                          <button type="button" disabled={editDisabled} onClick={() => setEditor({ user, app, kind: 'remaining' })} className={editButtonClass}>设置今日剩余</button>
                        </div>
                      </>
                    ) : (
                      <div className="mt-4 space-y-2">
                        <p className="text-sm text-neutral-300">原有剩余额度：<span className="font-semibold text-indigo-300">{app.quota}</span></p>
                        <p className="text-xs leading-5 text-amber-200/80">该应用暂未接入每日配置管理，此处仅供查看。</p>
                      </div>
                    )}
                  </div>
                );
              })}
              {user.apps.length === 0 && <p className="text-sm text-neutral-500">该用户暂无应用。</p>}
            </div>
          </article>
        ))}
      </div>
      {editor && <QuotaEditor edit={editor} onClose={() => setEditor(null)} onSave={saveQuota} />}
    </section>
  );
}
