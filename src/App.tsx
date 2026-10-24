import React, { useState, useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useNavigate } from 'react-router-dom';
import axios from 'axios';
import { LayoutDashboard, Users, LogOut, ShieldAlert, Cpu, BookOpen } from 'lucide-react';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts';
import StoryConfig from './StoryConfig';
import UsersView from './UsersView';

const SSO_BASE_URL = 'https://lazylady.cc/sso';

// --- Auth Utils ---
const setToken = (token: string) => localStorage.setItem('sso_admin_token', token);
const getToken = () => localStorage.getItem('sso_admin_token');
const removeToken = () => localStorage.removeItem('sso_admin_token');
const parseJwt = (token: string) => {
  try {
    return JSON.parse(atob(token.split('.')[1]));
  } catch (e) {
    return null;
  }
};
const isAdmin = () => {
  const token = getToken();
  if (!token) return false;
  const payload = parseJwt(token);
  return payload?.is_admin === true;
};

// --- API ---
const api = axios.create({
  baseURL: SSO_BASE_URL,
});
api.interceptors.request.use((config) => {
  const token = getToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// --- Components ---

function Login() {
  const [isRegister, setIsRegister] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  // On mount, check if there's a token and a redirect_uri. If so, immediately redirect.
  // This allows seamless SSO if the user is already logged in to the auth center.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const redirectUri = params.get('redirect_uri');
    const token = getToken();
    if (token && redirectUri) {
      window.location.href = `${redirectUri}${redirectUri.includes('?') ? '&' : '?'}token=${token}`;
    }
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      let token = '';
      if (isRegister) {
        // Register API expects JSON
        const res = await axios.post(`${SSO_BASE_URL}/auth/register`, {
          username,
          password,
          role: 'parent' // default role
        });
        token = res.data.access_token;
      } else {
        // Login API expects form data
        const params = new URLSearchParams();
        params.append('username', username);
        params.append('password', password);
        const res = await axios.post(`${SSO_BASE_URL}/auth/login`, params);
        token = res.data.access_token;
      }

      setToken(token);
      
      const params = new URLSearchParams(window.location.search);
      const redirectUri = params.get('redirect_uri');
      
      if (redirectUri) {
        // Centralized SSO redirect
        window.location.href = `${redirectUri}${redirectUri.includes('?') ? '&' : '?'}token=${token}`;
        return;
      }

      // If no redirect_uri, they are trying to log into the Admin Dashboard
      if (!isAdmin()) {
        removeToken();
        setError('Access denied. Administrator privileges required for dashboard access.');
        setLoading(false);
        return;
      }
      
      navigate('/');
    } catch (err: any) {
      setError(err.response?.data?.detail || (isRegister ? 'Registration failed' : 'Login failed'));
    }
    setLoading(false);
  };

  const isSSO = new URLSearchParams(window.location.search).has('redirect_uri');

  return (
    <div className="min-h-screen flex items-center justify-center bg-neutral-950 px-4">
      <div className="w-full max-w-md bg-neutral-900 border border-neutral-800 rounded-2xl p-8 shadow-2xl">
        <div className="flex flex-col items-center mb-8">
          <div className="w-12 h-12 bg-indigo-500/20 text-indigo-400 rounded-xl flex items-center justify-center mb-4">
            <ShieldAlert size={24} />
          </div>
          <h2 className="text-2xl font-bold text-white">
            {isSSO ? 'LazyLady Accounts' : 'Admin Console'}
          </h2>
          <p className="text-neutral-400 text-sm mt-2">
            {isSSO ? 'One account for all LazyLady apps' : 'Sign in to central command'}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-neutral-400 mb-1">Username</label>
            <input 
              type="text" 
              value={username}
              onChange={e => setUsername(e.target.value)}
              className="w-full bg-neutral-950 border border-neutral-800 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-indigo-500 transition-colors"
              required 
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-neutral-400 mb-1">Password</label>
            <input 
              type="password" 
              value={password}
              onChange={e => setPassword(e.target.value)}
              className="w-full bg-neutral-950 border border-neutral-800 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-indigo-500 transition-colors"
              required 
            />
          </div>
          
          {error && <div className="text-red-400 text-sm bg-red-400/10 p-3 rounded-lg border border-red-400/20">{error}</div>}
          
          <button 
            type="submit" 
            disabled={loading}
            className="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-medium py-2.5 rounded-lg transition-colors mt-4 disabled:opacity-50"
          >
            {loading ? (isRegister ? 'Registering...' : 'Authenticating...') : (isRegister ? 'Sign Up' : 'Sign In')}
          </button>

          {isSSO && (
            <div className="text-center mt-4 text-sm">
              <span className="text-neutral-500">
                {isRegister ? 'Already have an account?' : 'Need an account?'}
              </span>
              <button
                type="button"
                onClick={() => setIsRegister(!isRegister)}
                className="ml-2 text-indigo-400 hover:text-indigo-300 font-medium"
              >
                {isRegister ? 'Sign In' : 'Sign Up'}
              </button>
            </div>
          )}
        </form>
      </div>
    </div>
  );
}

function Dashboard() {
  const [stats, setStats] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<'overview' | 'users' | 'story-config'>('overview');
  const navigate = useNavigate();

  useEffect(() => {
    const fetchStats = async () => {
      try {
        const res = await api.get('/admin/dashboard/stats');
        setStats(res.data);
      } catch (err) {
        if (axios.isAxiosError(err) && err.response?.status === 401) {
          removeToken();
          navigate('/login');
        }
      } finally {
        setLoading(false);
      }
    };
    fetchStats();
  }, [navigate]);

  if (loading) return <div className="min-h-screen flex items-center justify-center">Loading core systems...</div>;

  const chartData = stats ? Object.keys(stats.users_per_app).map(key => ({
    name: key,
    users: stats.users_per_app[key]
  })) : [];

  return (
    <div className="min-h-screen bg-neutral-950 flex flex-col md:flex-row">
      {/* Sidebar */}
      <aside className="w-full md:w-64 md:shrink-0 border-b md:border-b-0 md:border-r border-neutral-800 bg-neutral-900/50 p-6 flex flex-col">
        <div className="flex items-center gap-3 mb-10 text-white font-bold text-lg">
          <Cpu className="text-indigo-400" />
          LazyLady Admin
        </div>
        
        <nav className="flex-1 space-y-2">
          <a href="#" onClick={() => setView('overview')} className={`flex items-center gap-3 px-4 py-2.5 rounded-lg font-medium ${view === 'overview' ? 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/20' : 'text-neutral-400 hover:bg-neutral-800 hover:text-white transition-colors'}`}>
            <LayoutDashboard size={18} /> Overview
          </a>
          <a href="#" onClick={() => setView('users')} className={`flex items-center gap-3 px-4 py-2.5 rounded-lg font-medium ${view === 'users' ? 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/20' : 'text-neutral-400 hover:bg-neutral-800 hover:text-white transition-colors'}`}>
            <Users size={18} /> Users & Quotas
          </a>
          <a href="#" onClick={() => setView('story-config')} className={`flex items-center gap-3 px-4 py-2.5 rounded-lg font-medium ${view === 'story-config' ? 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/20' : 'text-neutral-400 hover:bg-neutral-800 hover:text-white transition-colors'}`}>
            <BookOpen size={18} /> Story项目配置
          </a>
        </nav>
        
        <button 
          onClick={() => { removeToken(); navigate('/login'); }}
          className="flex items-center gap-3 px-4 py-2.5 text-neutral-500 hover:text-red-400 transition-colors mt-auto"
        >
          <LogOut size={18} /> Sign Out
        </button>
      </aside>

      {/* Main Content */}
      <main className="min-w-0 flex-1 p-4 md:p-8">
        {view === 'overview' ? (
          <>
            <h1 className="text-2xl font-bold text-white mb-8">System Overview</h1>
            
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
              <div className="bg-neutral-900 border border-neutral-800 p-6 rounded-2xl">
                <h3 className="text-neutral-400 text-sm font-medium mb-2">Total Registered Users</h3>
                <div className="text-4xl font-black text-white">{stats?.total_users || 0}</div>
              </div>
              <div className="bg-neutral-900 border border-neutral-800 p-6 rounded-2xl">
                <h3 className="text-neutral-400 text-sm font-medium mb-2">Active Products</h3>
                <div className="text-4xl font-black text-white">{chartData.length}</div>
              </div>
            </div>

            <div className="bg-neutral-900 border border-neutral-800 p-6 rounded-2xl">
              <h3 className="text-neutral-400 text-sm font-medium mb-6">User Distribution per Product</h3>
              <div className="h-[300px] w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#262626" vertical={false} />
                    <XAxis dataKey="name" stroke="#525252" axisLine={false} tickLine={false} />
                    <YAxis stroke="#525252" axisLine={false} tickLine={false} />
                    <Tooltip 
                      contentStyle={{ backgroundColor: '#171717', border: '1px solid #262626', borderRadius: '8px' }}
                      itemStyle={{ color: '#818cf8' }}
                    />
                    <Bar dataKey="users" fill="#6366f1" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          </>
        ) : view === 'users' ? (
          <UsersView api={api} />
        ) : (
          <StoryConfig />
        )}
      </main>
    </div>
  );
}

const ProtectedRoute = ({ children }: { children: React.ReactNode }) => {
  if (!isAdmin()) return <Navigate to="/login" replace />;
  return <>{children}</>;
};

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route 
          path="/*" 
          element={
            <ProtectedRoute>
              <Dashboard />
            </ProtectedRoute>
          } 
        />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
