import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import axios from 'axios';
import { AlertCircle, CheckCircle2, Loader2, RefreshCw, Save, ShieldCheck } from 'lucide-react';

const STORY_ADMIN_CONFIG_URL = 'https://story.lazylady.cc';
const PAGE_COUNTS = [4, 5, 6, 7, 8, 9, 10] as const;
const STYLES = [
  ['puppet', '纸偶拼贴'],
  ['watercolor', '水彩绘本'],
  ['comic', '儿童连环画'],
  ['xiaohei', '留白手绘'],
  ['rough', '草图白板'],
  ['kawaii', '软萌几何'],
  ['doodle', '涂鸦动作'],
] as const;

type PageCount = (typeof PAGE_COUNTS)[number];
type StoryStyle = (typeof STYLES)[number][0];
type Provider = 'ark' | 'aliyun';

type ProjectConfig = {
  name: string;
  aiEnabled: boolean;
  defaultPageCount: PageCount;
  defaultStyle: StoryStyle;
};

type StoryConfigResponse = {
  revision: number;
  project: ProjectConfig;
  values: Record<string, string>;
  secrets: Record<string, boolean>;
  updatedAt: string | null;
};

type StoryAdminSession = {
  token: string;
  expiresAt: number;
};

type ConfigValueKey =
  | 'AI_PROVIDER'
  | 'ARK_LLM_MODEL'
  | 'ARK_ASR_MODEL'
  | 'ARK_IMAGE_MODEL'
  | 'ARK_IMAGE_SIZE'
  | 'ARK_BASE_URL'
  | 'DASHSCOPE_LLM_MODEL'
  | 'DASHSCOPE_ASR_MODEL'
  | 'DASHSCOPE_IMAGE_MODEL'
  | 'DASHSCOPE_IMAGE_SIZE'
  | 'DASHSCOPE_BASE_URL'
  | 'DASHSCOPE_MULTIMODAL_BASE_URL'
  | 'DASHSCOPE_WORKSPACE_ID'
  | 'DASHSCOPE_TTS_MODEL'
  | 'DASHSCOPE_TTS_WS_URL'
  | 'VOLC_TTS_APPID'
  | 'VOLC_TTS_SPEAKER'
  | 'VOLC_TTS_URL'
  | 'VOLC_TTS_RESOURCE_ID';

type SecretKey = 'ARK_API_KEY' | 'DASHSCOPE_API_KEY' | 'VOLC_TTS_ACCESS_KEY';

type FieldDefinition = {
  key: ConfigValueKey;
  label: string;
  placeholder?: string;
  hint?: string;
};

const ARK_FIELDS: FieldDefinition[] = [
  { key: 'ARK_LLM_MODEL', label: 'LLM 模型', placeholder: '例如 doubao-seed-1-6-250615' },
  { key: 'ARK_ASR_MODEL', label: '语音识别模型', hint: '留空复用文本模型；模型须支持音频输入' },
  { key: 'ARK_IMAGE_MODEL', label: '图片模型', placeholder: '填写账号已开通的图片模型' },
  { key: 'ARK_IMAGE_SIZE', label: '图片尺寸', placeholder: '例如 2048x2048' },
  { key: 'ARK_BASE_URL', label: 'API 地址', placeholder: 'https://ark.cn-beijing.volces.com/api/v3' },
];

const ALIYUN_FIELDS: FieldDefinition[] = [
  { key: 'DASHSCOPE_LLM_MODEL', label: 'LLM 模型', placeholder: '例如 qwen-plus' },
  { key: 'DASHSCOPE_ASR_MODEL', label: '语音识别模型', placeholder: '留空使用服务默认模型' },
  { key: 'DASHSCOPE_IMAGE_MODEL', label: '图片模型', placeholder: '留空使用服务默认模型' },
  { key: 'DASHSCOPE_IMAGE_SIZE', label: '图片尺寸', placeholder: '例如 1024*1024' },
  { key: 'DASHSCOPE_BASE_URL', label: 'API 地址', placeholder: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { key: 'DASHSCOPE_MULTIMODAL_BASE_URL', label: '多模态 API 地址', placeholder: 'https://dashscope.aliyuncs.com/api/v1' },
  { key: 'DASHSCOPE_WORKSPACE_ID', label: 'Workspace ID', placeholder: '可选' },
  { key: 'DASHSCOPE_TTS_MODEL', label: 'TTS 模型', placeholder: '留空使用 sambert-zhide-v1' },
  { key: 'DASHSCOPE_TTS_WS_URL', label: 'TTS WebSocket 地址', placeholder: 'wss://dashscope.aliyuncs.com/api-ws/v1/inference' },
];

const TTS_FIELDS: FieldDefinition[] = [
  { key: 'VOLC_TTS_APPID', label: '火山 TTS App ID', placeholder: '可选' },
  { key: 'VOLC_TTS_SPEAKER', label: '火山 TTS 音色', placeholder: '例如 zh_female_cancan_mars_bigtts' },
  { key: 'VOLC_TTS_URL', label: '火山 TTS 地址', placeholder: 'https://openspeech.bytedance.com/api/v3/tts/unidirectional' },
  { key: 'VOLC_TTS_RESOURCE_ID', label: '火山 TTS Resource ID', placeholder: '可选' },
];

const SECRET_FIELDS: Array<{ key: SecretKey; label: string; hint: string }> = [
  { key: 'ARK_API_KEY', label: '火山方舟 API Key', hint: '用于 LLM、语音和图片服务' },
  { key: 'DASHSCOPE_API_KEY', label: '阿里云百炼 API Key', hint: '用于通义模型和语音服务' },
  { key: 'VOLC_TTS_ACCESS_KEY', label: '火山 TTS Access Key', hint: '用于火山 TTS 鉴权' },
];

const configApi = axios.create({
  baseURL: STORY_ADMIN_CONFIG_URL,
  timeout: 15_000,
});

const isRecord = (value: unknown): value is Record<string, unknown> => (
  typeof value === 'object' && value !== null
);

const isConfigResponse = (value: unknown): value is StoryConfigResponse => {
  if (!isRecord(value) || !Number.isInteger(value.revision) || !isRecord(value.project)) return false;
  const project = value.project;
  return typeof project.name === 'string' && typeof project.aiEnabled === 'boolean' &&
    PAGE_COUNTS.includes(project.defaultPageCount as PageCount) && STYLES.some(([key]) => key === project.defaultStyle) &&
    isRecord(value.values) && isRecord(value.secrets);
};

const isSessionResponse = (value: unknown): value is StoryAdminSession => (
  isRecord(value) && typeof value.token === 'string' && /^[A-Za-z0-9_-]{43}$/.test(value.token) &&
  typeof value.expiresAt === 'number' && Number.isFinite(value.expiresAt) && value.expiresAt > Date.now()
);

const normalizeStringRecord = (value: unknown): Record<string, string> => {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.entries(value).filter(([, item]) => typeof item === 'string'),
  ) as Record<string, string>;
};

const normalizeBooleanRecord = (value: unknown): Record<string, boolean> => {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.entries(value).filter(([, item]) => typeof item === 'boolean'),
  ) as Record<string, boolean>;
};

const normalizeProject = (value: Record<string, unknown>): ProjectConfig => {
  const pageCount = PAGE_COUNTS.includes(value.defaultPageCount as PageCount)
    ? value.defaultPageCount as PageCount
    : 4;
  const style = STYLES.some(([key]) => key === value.defaultStyle)
    ? value.defaultStyle as StoryStyle
    : 'puppet';

  return {
    name: typeof value.name === 'string' ? value.name : '',
    aiEnabled: value.aiEnabled === true,
    defaultPageCount: pageCount,
    defaultStyle: style,
  };
};

const getErrorMessage = (error: unknown, fallback: string): string => {
  if (!axios.isAxiosError(error)) return error instanceof Error ? error.message : fallback;
  if (error.response?.status === 401) return 'Story 管理会话已过期，请重新验证。';
  if (error.response?.status === 403) return '当前账号没有 Story 配置权限。';
  const responseData = error.response?.data;
  if (isRecord(responseData) && typeof responseData.error === 'string') return responseData.error;
  if (error.response?.status === 409) return '配置已被其他管理员更新，请重新加载后再保存。';
  if (error.code === 'ECONNABORTED') return '请求超时，请检查 Story 服务后重试。';
  return error.message || fallback;
};

function ConfigField({
  field,
  value,
  disabled,
  onChange,
}: {
  field: FieldDefinition;
  value: string;
  disabled: boolean;
  onChange: (key: ConfigValueKey, value: string) => void;
}) {
  return (
    <label className="block space-y-2">
      <span className="text-sm font-medium text-neutral-200">{field.label}</span>
      <input
        type="text"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(field.key, event.target.value)}
        placeholder={field.placeholder}
        className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2.5 text-sm text-white outline-none transition-colors placeholder:text-neutral-600 focus:border-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
      />
      {field.hint && <span className="block text-xs text-neutral-500">{field.hint}</span>}
    </label>
  );
}

export default function StoryConfig() {
  const [session, setSession] = useState<StoryAdminSession | null>(null);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [authLoading, setAuthLoading] = useState(false);
  const [authError, setAuthError] = useState('');
  const [revision, setRevision] = useState<number | null>(null);
  const [project, setProject] = useState<ProjectConfig>({
    name: '',
    aiEnabled: false,
    defaultPageCount: 4,
    defaultStyle: 'puppet',
  });
  const [values, setValues] = useState<Record<string, string>>({});
  const [configuredSecrets, setConfiguredSecrets] = useState<Record<string, boolean>>({});
  const [secretDrafts, setSecretDrafts] = useState<Partial<Record<SecretKey, string>>>({});
  const [clearSecrets, setClearSecrets] = useState<Partial<Record<SecretKey, boolean>>>({});
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [conflict, setConflict] = useState(false);

  const expireSession = useCallback((message: string) => {
    setSession(null);
    setRevision(null);
    setLoading(false);
    setSaving(false);
    setError('');
    setSuccess('');
    setAuthError(message);
  }, []);

  const applyConfig = useCallback((payload: StoryConfigResponse) => {
    setRevision(payload.revision);
    setProject(normalizeProject(payload.project as unknown as Record<string, unknown>));
    setValues(normalizeStringRecord(payload.values));
    setConfiguredSecrets(normalizeBooleanRecord(payload.secrets));
    setSecretDrafts({});
    setClearSecrets({});
    setUpdatedAt(payload.updatedAt ?? null);
    setConflict(false);
  }, []);

  const loadConfig = useCallback(async (activeSession: StoryAdminSession) => {
    if (Date.now() >= activeSession.expiresAt) {
      expireSession('Story 管理会话已过期，请重新验证。');
      return;
    }
    setLoading(true);
    setError('');
    setSuccess('');
    try {
      const response = await configApi.get('/api/admin/config', {
        headers: { Authorization: `Bearer ${activeSession.token}` },
      });
      if (!isConfigResponse(response.data)) throw new Error('服务器返回的配置格式无效。');
      applyConfig(response.data);
    } catch (requestError) {
      if (axios.isAxiosError(requestError) && requestError.response?.status === 401) {
        expireSession('Story 管理会话已过期，请重新验证。');
        return;
      }
      setError(getErrorMessage(requestError, '加载 Story 配置失败。'));
    } finally {
      setLoading(false);
    }
  }, [applyConfig, expireSession]);

  useEffect(() => {
    if (session) void loadConfig(session);
  }, [loadConfig, session]);

  const submitSession = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAuthError('');
    setAuthLoading(true);
    const submittedUsername = username.trim();
    const submittedPassword = password;
    try {
      const response = await configApi.post('/api/admin/session', {
        username: submittedUsername,
        password: submittedPassword,
      });
      if (!isSessionResponse(response.data)) throw new Error('服务器返回的管理会话无效。');
      setSession(response.data);
      setRevision(null);
      setError('');
      setSuccess('');
    } catch (requestError) {
      setAuthError(getErrorMessage(requestError, '管理员账号验证失败，请重试。'));
    } finally {
      setPassword('');
      setAuthLoading(false);
    }
  };

  const disabled = loading || saving || revision === null || conflict;
  const activeProvider: Provider = ['aliyun', 'dashscope', 'bailian'].includes(values.AI_PROVIDER) ? 'aliyun' : 'ark';
  const providerFields = activeProvider === 'ark' ? ARK_FIELDS : ALIYUN_FIELDS;
  const updatedLabel = useMemo(() => {
    if (!updatedAt) return '尚未记录更新时间';
    const date = new Date(updatedAt);
    return Number.isNaN(date.getTime()) ? updatedAt : `上次更新：${date.toLocaleString('zh-CN')}`;
  }, [updatedAt]);

  const updateValue = (key: ConfigValueKey, value: string) => {
    setSuccess('');
    setValues((current) => ({ ...current, [key]: value }));
  };

  const updateProject = <K extends keyof ProjectConfig>(key: K, value: ProjectConfig[K]) => {
    setSuccess('');
    setProject((current) => ({ ...current, [key]: value }));
  };

  const updateSecretDraft = (key: SecretKey, value: string) => {
    setSuccess('');
    setSecretDrafts((current) => ({ ...current, [key]: value }));
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (revision === null || saving || !session) return;
    if (Date.now() >= session.expiresAt) {
      expireSession('Story 管理会话已过期，请重新验证。');
      return;
    }

    const keysToClear = SECRET_FIELDS
      .map(({ key }) => key)
      .filter((key) => clearSecrets[key] === true);
    if (keysToClear.length > 0) {
      const confirmed = window.confirm(`确认清除以下密钥吗？\n${keysToClear.join('、')}\n此操作保存后无法恢复。`);
      if (!confirmed) return;
    }

    setSaving(true);
    setError('');
    setSuccess('');
    setConflict(false);
    const secrets = Object.fromEntries(
      SECRET_FIELDS
        .map(({ key }) => [key, secretDrafts[key]?.trim() ?? ''])
        .filter(([, value]) => value.length > 0),
    ) as Record<string, string>;
    const payload = {
      revision,
      project,
      values,
      secrets,
      ...(keysToClear.length > 0 ? { clearSecrets: keysToClear } : {}),
    };

    try {
      const response = await configApi.put('/api/admin/config', payload, {
        headers: { Authorization: `Bearer ${session.token}` },
      });
      if (!isConfigResponse(response.data)) throw new Error('服务器返回的保存结果无效。');
      applyConfig(response.data);
      setSuccess('Story 配置已保存。');
    } catch (requestError) {
      if (axios.isAxiosError(requestError) && requestError.response?.status === 401) {
        expireSession('Story 管理会话已过期，请重新验证。');
        return;
      }
      if (axios.isAxiosError(requestError) && requestError.response?.status === 409) {
        setConflict(true);
      }
      setError(getErrorMessage(requestError, '保存 Story 配置失败。'));
    } finally {
      setSaving(false);
    }
  };

  if (!session) {
    return (
      <div className="max-w-xl">
        <div className="mb-8">
          <h1 className="text-2xl font-bold text-white">Story 项目配置</h1>
          <p className="mt-2 text-sm text-neutral-400">模型、默认项目参数和服务密钥由管理端统一维护。</p>
        </div>
        <div className="rounded-2xl border border-neutral-800 bg-neutral-900 p-6 shadow-xl">
          <div className="mb-6 flex items-start gap-3 rounded-xl border border-indigo-400/20 bg-indigo-400/10 p-4 text-sm text-indigo-200">
            <ShieldCheck size={18} className="mt-0.5 shrink-0 text-indigo-300" />
            <span>Story 配置需要再次验证管理账号，会话有效 15 分钟。</span>
          </div>
          {authError && (
            <div role="alert" className="mb-5 flex items-center gap-2 rounded-xl border border-red-400/20 bg-red-400/10 p-4 text-sm text-red-300">
              <AlertCircle size={17} />{authError}
            </div>
          )}
          <form onSubmit={submitSession} className="space-y-5">
            <label className="block space-y-2">
              <span className="text-sm font-medium text-neutral-200">管理员账号</span>
              <input
                required
                type="text"
                value={username}
                disabled={authLoading}
                autoComplete="username"
                onChange={(event) => setUsername(event.target.value)}
                className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2.5 text-sm text-white outline-none transition-colors placeholder:text-neutral-600 focus:border-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
              />
            </label>
            <label className="block space-y-2">
              <span className="text-sm font-medium text-neutral-200">管理员密码</span>
              <input
                required
                type="password"
                value={password}
                disabled={authLoading}
                autoComplete="current-password"
                onChange={(event) => setPassword(event.target.value)}
                className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2.5 text-sm text-white outline-none transition-colors placeholder:text-neutral-600 focus:border-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
              />
            </label>
            <button type="submit" disabled={authLoading} className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-indigo-600 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50">
              {authLoading ? <Loader2 size={16} className="animate-spin" /> : <ShieldCheck size={16} />}
              {authLoading ? '正在验证…' : '创建管理会话'}
            </button>
          </form>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-5xl">
      <div className="mb-8 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white">Story 项目配置</h1>
          <p className="mt-2 text-sm text-neutral-400">管理故事生成项目的默认设置、模型参数和服务密钥。</p>
        </div>
        <div className="flex items-center gap-2 text-xs text-neutral-500">
          <ShieldCheck size={15} className="text-emerald-400" />
          密钥只显示是否已配置
        </div>
      </div>

      {loading && (
        <div className="mb-6 flex items-center gap-3 rounded-xl border border-neutral-800 bg-neutral-900 p-4 text-sm text-neutral-300">
          <Loader2 size={17} className="animate-spin text-indigo-400" /> 正在加载 Story 配置…
        </div>
      )}
      {error && (
        <div role="alert" className="mb-6 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-red-400/20 bg-red-400/10 p-4 text-sm text-red-300">
          <span className="flex items-center gap-2"><AlertCircle size={17} />{error}</span>
          {!loading && (
            <button type="button" onClick={() => void loadConfig(session)} disabled={loading || saving} className="inline-flex items-center gap-2 rounded-lg border border-red-300/30 px-3 py-2 text-xs font-medium text-red-200 hover:bg-red-300/10 disabled:opacity-50">
              <RefreshCw size={14} />重新加载
            </button>
          )}
        </div>
      )}
      {success && (
        <div role="status" className="mb-6 flex items-center gap-2 rounded-xl border border-emerald-400/20 bg-emerald-400/10 p-4 text-sm text-emerald-300">
          <CheckCircle2 size={17} />{success}
        </div>
      )}

      <form onSubmit={submit} className="space-y-6">
        <section className="rounded-2xl border border-neutral-800 bg-neutral-900 p-6">
          <div className="mb-5">
            <h2 className="text-base font-semibold text-white">项目默认值</h2>
            <p className="mt-1 text-xs text-neutral-500">新建故事时使用这些设置，已有项目不会被覆盖。</p>
          </div>
          <div className="grid gap-5 md:grid-cols-2">
            <label className="block space-y-2">
              <span className="text-sm font-medium text-neutral-200">项目名称</span>
              <input required maxLength={80} type="text" value={project.name} disabled={disabled} onChange={(event) => updateProject('name', event.target.value)} className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2.5 text-sm text-white outline-none focus:border-indigo-500 disabled:cursor-not-allowed disabled:opacity-50" />
            </label>
            <label className="flex items-center gap-3 self-end rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-3 text-sm text-neutral-200">
              <input type="checkbox" checked={project.aiEnabled} disabled={disabled} onChange={(event) => updateProject('aiEnabled', event.target.checked)} className="h-4 w-4 accent-indigo-500" />
              启用 AI 功能
            </label>
            <label className="block space-y-2">
              <span className="text-sm font-medium text-neutral-200">默认页数</span>
              <select value={project.defaultPageCount} disabled={disabled} onChange={(event) => updateProject('defaultPageCount', Number(event.target.value) as PageCount)} className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2.5 text-sm text-white outline-none focus:border-indigo-500 disabled:cursor-not-allowed disabled:opacity-50">
                {PAGE_COUNTS.map((count) => <option key={count} value={count}>{count} 页</option>)}
              </select>
            </label>
            <label className="block space-y-2">
              <span className="text-sm font-medium text-neutral-200">默认画风</span>
              <select value={project.defaultStyle} disabled={disabled} onChange={(event) => updateProject('defaultStyle', event.target.value as StoryStyle)} className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2.5 text-sm text-white outline-none focus:border-indigo-500 disabled:cursor-not-allowed disabled:opacity-50">
                {STYLES.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
              </select>
            </label>
          </div>
        </section>

        <section className="rounded-2xl border border-neutral-800 bg-neutral-900 p-6">
          <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-white">模型与服务</h2>
              <p className="mt-1 text-xs text-neutral-500">选择 AI 服务商并填写对应的模型参数。</p>
            </div>
            <div className="rounded-lg border border-neutral-800 bg-neutral-950 p-1" role="tablist" aria-label="AI 服务商">
              {([['ark', '火山方舟'], ['aliyun', '阿里云百炼']] as const).map(([key, label]) => (
                <button key={key} type="button" role="tab" aria-selected={activeProvider === key} disabled={disabled} onClick={() => updateValue('AI_PROVIDER', key)} className={`rounded-md px-3 py-2 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${activeProvider === key ? 'bg-indigo-500/20 text-indigo-300' : 'text-neutral-500 hover:text-neutral-200'}`}>
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="grid gap-5 md:grid-cols-2">
            {providerFields.map((field) => <ConfigField key={field.key} field={field} value={values[field.key] ?? ''} disabled={disabled} onChange={updateValue} />)}
          </div>
        </section>

        <section className="rounded-2xl border border-neutral-800 bg-neutral-900 p-6">
          <div className="mb-5">
            <h2 className="text-base font-semibold text-white">语音服务参数</h2>
            <p className="mt-1 text-xs text-neutral-500">火山方舟的朗读音频配置。地址、音色和资源ID留空使用服务默认值；停用请清除对应密钥。</p>
          </div>
          <div className="grid gap-5 md:grid-cols-2">
            {TTS_FIELDS.map((field) => <ConfigField key={field.key} field={field} value={values[field.key] ?? ''} disabled={disabled} onChange={updateValue} />)}
          </div>
        </section>

        <section className="rounded-2xl border border-neutral-800 bg-neutral-900 p-6">
          <div className="mb-5">
            <h2 className="text-base font-semibold text-white">服务密钥</h2>
            <p className="mt-1 text-xs text-neutral-500">已配置的密钥不会回显。输入新值后保存即可替换，留空会保留原值。</p>
          </div>
          <div className="space-y-5">
            {SECRET_FIELDS.map(({ key, label, hint }) => {
              const isConfigured = configuredSecrets[key] === true;
              const willClear = clearSecrets[key] === true;
              return (
                <div key={key} className="rounded-xl border border-neutral-800 bg-neutral-950/60 p-4">
                  <label className="block space-y-2">
                    <span className="text-sm font-medium text-neutral-200">{label}</span>
                    <input type="password" value={secretDrafts[key] ?? ''} disabled={disabled || willClear} onChange={(event) => updateSecretDraft(key, event.target.value)} placeholder={isConfigured ? '已配置，留空保留' : '未配置，请输入'} autoComplete="new-password" className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2.5 text-sm text-white outline-none placeholder:text-neutral-600 focus:border-indigo-500 disabled:cursor-not-allowed disabled:opacity-50" />
                    <span className="block text-xs text-neutral-500">{hint}</span>
                  </label>
                  {isConfigured && (
                    <label className="mt-3 flex items-center gap-2 text-xs text-neutral-400">
                      <input type="checkbox" checked={willClear} disabled={disabled} onChange={(event) => {
                        const checked = event.target.checked;
                        setClearSecrets((current) => ({ ...current, [key]: checked }));
                        if (checked) updateSecretDraft(key, '');
                      }} className="h-3.5 w-3.5 accent-red-500" />
                      清除这个密钥（保存时需要再次确认）
                    </label>
                  )}
                </div>
              );
            })}
          </div>
        </section>

        <div className="flex flex-wrap items-center justify-between gap-4 pb-4">
          <span className="text-xs text-neutral-500">{updatedLabel}{revision !== null && ` · 配置版本 ${revision}`}</span>
          <button type="submit" disabled={disabled} className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50">
            {saving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
            {saving ? '保存中…' : '保存配置'}
          </button>
        </div>
      </form>
    </div>
  );
}
