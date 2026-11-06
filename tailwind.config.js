/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx,html}'],
  theme: {
    extend: {
      colors: {
        krypt: {
          black: '#000000',
          void: '#0A0A0F',
          // Translucent on purpose: the app sits on a living backdrop
          // (components/glass/GlassBackdrop), and an opaque surface is a hole
          // in it. Tailwind keeps an explicit `/NN` modifier working on
          // rgb()-with-alpha tokens, so `bg-krypt-surface/95` still means 95%.
          // Same hues as the old #11111A / #171722, a touch bluer.
          surface: 'rgb(17 18 28 / 0.86)',
          surface2: 'rgb(24 26 40 / 0.74)',
          border: 'rgba(255,255,255,0.08)',
          borderHi: 'rgba(255,255,255,0.16)',
          muted: '#A1A1AA',
          dim: '#71717A',
          // Polymarket-blue accent ramp (deep blue -> blue -> azure). The
          // token names (indigo/purple/pink) are kept so every component
          // that references them re-themes centrally.
          indigo: '#1D4ED8',
          purple: '#3B82F6',
          pink: '#38BDF8',
          win: '#22C55E',
          loss: '#EF4444',
          warn: '#F59E0B',
        },
      },
      fontFamily: {
        sans: ['"Chakra Petch"', 'Inter', 'system-ui', 'sans-serif'],
        pixel: ['"Press Start 2P"', 'monospace'],
        mono: ['"JetBrains Mono"', 'Menlo', 'monospace'],
      },
      backgroundImage: {
        'krypt-glow':
          'linear-gradient(90deg, #1D4ED8 0%, #3B82F6 50%, #38BDF8 100%)',
        'krypt-radial':
          'radial-gradient(700px circle at 15% 0%, rgba(59,130,246,0.14), transparent 60%)',
        'krypt-radial-r':
          'radial-gradient(600px circle at 90% 0%, rgba(56,189,248,0.10), transparent 60%)',
      },
      boxShadow: {
        'krypt-glow': '0 0 28px 0 rgba(59,130,246,0.35)',
        'krypt-soft': '0 10px 40px -10px rgba(59,130,246,0.20)',
        'krypt-strong': '0 0 60px -10px rgba(59,130,246,0.55)',
      },
      keyframes: {
        'fade-in': {
          '0%': { opacity: '0', transform: 'translateY(4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'gradient-x': {
          '0%,100%': { backgroundPosition: '0% 50%' },
          '50%': { backgroundPosition: '100% 50%' },
        },
        'pulse-slow': {
          '0%,100%': { opacity: '0.4' },
          '50%': { opacity: '0.9' },
        },
      },
      animation: {
        'fade-in': 'fade-in 280ms ease-out',
        'gradient-x': 'gradient-x 6s ease infinite',
        'pulse-slow': 'pulse-slow 2.6s ease-in-out infinite',
      },
    },
  },
  plugins: [],
};
