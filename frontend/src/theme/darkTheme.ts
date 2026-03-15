import { createTheme } from '@mui/material/styles';

declare module '@mui/material/styles' {
  interface Palette {
    glass: {
      background: string;
      border: string;
      backgroundStrong: string;
      borderStrong: string;
    };
  }
  interface PaletteOptions {
    glass?: {
      background: string;
      border: string;
      backgroundStrong: string;
      borderStrong: string;
    };
  }
}

export const darkTheme = createTheme({
  palette: {
    mode: 'dark',
    background: {
      default: '#050508',
      paper: '#0D0B2B',
    },
    primary: {
      main: '#7C3AED',
      light: '#A78BFA',
      dark: '#5B21B6',
    },
    secondary: {
      main: '#2563EB',
      light: '#60A5FA',
      dark: '#1D4ED8',
    },
    info: {
      main: '#06B6D4',
      light: '#67E8F9',
      dark: '#0E7490',
    },
    error: {
      main: '#F87171',
      light: '#FCA5A5',
      dark: '#DC2626',
    },
    success: {
      main: '#10B981',
      light: '#6EE7B7',
      dark: '#059669',
    },
    text: {
      primary: '#FFFFFF',
      secondary: 'rgba(255,255,255,0.65)',
      disabled: 'rgba(255,255,255,0.38)',
    },
    divider: 'rgba(255,255,255,0.10)',
    glass: {
      background: 'rgba(255,255,255,0.05)',
      border: 'rgba(255,255,255,0.10)',
      backgroundStrong: 'rgba(255,255,255,0.08)',
      borderStrong: 'rgba(255,255,255,0.15)',
    },
  },
  typography: {
    fontFamily: '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    h1: {
      fontWeight: 800,
      fontSize: '3.5rem',
      lineHeight: 1.15,
      letterSpacing: '-0.02em',
    },
    h2: {
      fontWeight: 700,
      fontSize: '2.5rem',
      lineHeight: 1.2,
      letterSpacing: '-0.015em',
    },
    h3: {
      fontWeight: 700,
      fontSize: '2rem',
      lineHeight: 1.25,
      letterSpacing: '-0.01em',
    },
    h4: {
      fontWeight: 600,
      fontSize: '1.5rem',
      lineHeight: 1.3,
    },
    h5: {
      fontWeight: 600,
      fontSize: '1.25rem',
      lineHeight: 1.4,
    },
    h6: {
      fontWeight: 600,
      fontSize: '1rem',
      lineHeight: 1.5,
    },
    body1: {
      fontSize: '1rem',
      lineHeight: 1.6,
    },
    body2: {
      fontSize: '0.875rem',
      lineHeight: 1.57,
    },
    button: {
      fontWeight: 700,
      letterSpacing: '0.08em',
      textTransform: 'uppercase',
    },
    caption: {
      fontSize: '0.75rem',
      lineHeight: 1.5,
      letterSpacing: '0.04em',
    },
  },
  shape: {
    borderRadius: 12,
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        '*': {
          boxSizing: 'border-box',
        },
        body: {
          backgroundColor: '#050508',
          color: '#FFFFFF',
          margin: 0,
          padding: 0,
        },
        '::-webkit-scrollbar': {
          width: '6px',
        },
        '::-webkit-scrollbar-track': {
          background: 'rgba(255,255,255,0.03)',
        },
        '::-webkit-scrollbar-thumb': {
          background: 'rgba(124,58,237,0.4)',
          borderRadius: '3px',
        },
        '::-webkit-scrollbar-thumb:hover': {
          background: 'rgba(124,58,237,0.6)',
        },
      },
    },
    MuiButton: {
      styleOverrides: {
        root: {
          borderRadius: '32px',
          textTransform: 'uppercase',
          fontWeight: 700,
          letterSpacing: '0.08em',
          transition: 'all 0.25s ease',
        },
        contained: {
          background: 'linear-gradient(135deg, #7C3AED 0%, #2563EB 100%)',
          color: '#FFFFFF',
          boxShadow: '0 0 20px rgba(124,58,237,0.4)',
          '&:hover': {
            background: 'linear-gradient(135deg, #6D28D9 0%, #1D4ED8 100%)',
            boxShadow: '0 0 32px rgba(124,58,237,0.6)',
            transform: 'translateY(-1px)',
          },
          '&:active': {
            transform: 'translateY(0)',
          },
          '&.Mui-disabled': {
            background: 'rgba(255,255,255,0.12)',
            color: 'rgba(255,255,255,0.38)',
            boxShadow: 'none',
          },
        },
        outlined: {
          borderColor: 'rgba(167,139,250,0.5)',
          backgroundColor: 'rgba(124,58,237,0.15)',
          color: '#A78BFA',
          '&:hover': {
            borderColor: 'rgba(167,139,250,0.8)',
            backgroundColor: 'rgba(124,58,237,0.25)',
          },
        },
        text: {
          color: 'rgba(255,255,255,0.65)',
          '&:hover': {
            backgroundColor: 'rgba(255,255,255,0.08)',
            color: '#FFFFFF',
          },
        },
        sizeLarge: {
          padding: '14px 36px',
          fontSize: '1rem',
        },
        sizeMedium: {
          padding: '10px 24px',
          fontSize: '0.875rem',
        },
        sizeSmall: {
          padding: '6px 16px',
          fontSize: '0.8125rem',
        },
      },
    },
    MuiTextField: {
      defaultProps: {
        variant: 'outlined',
      },
      styleOverrides: {
        root: {
          '& .MuiOutlinedInput-root': {
            backgroundColor: 'rgba(255,255,255,0.06)',
            borderRadius: '12px',
            '& fieldset': {
              borderColor: 'rgba(255,255,255,0.15)',
              transition: 'border-color 0.2s ease, box-shadow 0.2s ease',
            },
            '&:hover fieldset': {
              borderColor: 'rgba(167,139,250,0.5)',
            },
            '&.Mui-focused fieldset': {
              borderColor: '#7C3AED',
              borderWidth: '1.5px',
              boxShadow: '0 0 0 3px rgba(124,58,237,0.15)',
            },
            '& input': {
              color: '#FFFFFF',
            },
            '& input::placeholder': {
              color: 'rgba(255,255,255,0.38)',
              opacity: 1,
            },
          },
          '& .MuiInputLabel-root': {
            color: 'rgba(255,255,255,0.5)',
            '&.Mui-focused': {
              color: '#A78BFA',
            },
          },
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: {
          backgroundColor: 'rgba(124,58,237,0.25)',
          border: '1px solid rgba(167,139,250,0.4)',
          color: '#A78BFA',
          borderRadius: '20px',
          fontWeight: 500,
          '& .MuiChip-deleteIcon': {
            color: 'rgba(167,139,250,0.6)',
            '&:hover': {
              color: '#F87171',
            },
          },
        },
        outlined: {
          backgroundColor: 'transparent',
          borderColor: 'rgba(167,139,250,0.4)',
        },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: 'none',
          backgroundColor: 'rgba(255,255,255,0.05)',
          border: '1px solid rgba(255,255,255,0.10)',
          backdropFilter: 'blur(20px)',
          borderRadius: '20px',
        },
        elevation1: {
          boxShadow: '0 4px 24px rgba(0,0,0,0.3)',
        },
        elevation2: {
          boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
        },
        elevation3: {
          boxShadow: '0 12px 40px rgba(0,0,0,0.5)',
        },
      },
    },
    MuiCard: {
      styleOverrides: {
        root: {
          backgroundImage: 'none',
          backgroundColor: 'rgba(255,255,255,0.05)',
          border: '1px solid rgba(255,255,255,0.10)',
          backdropFilter: 'blur(20px)',
          borderRadius: '24px',
          boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
        },
      },
    },
    MuiAppBar: {
      styleOverrides: {
        root: {
          backgroundColor: 'rgba(13,11,43,0.8)',
          backdropFilter: 'blur(20px)',
          borderBottom: '1px solid rgba(255,255,255,0.10)',
          boxShadow: 'none',
        },
      },
    },
    MuiIconButton: {
      styleOverrides: {
        root: {
          color: 'rgba(255,255,255,0.65)',
          transition: 'all 0.2s ease',
          '&:hover': {
            backgroundColor: 'rgba(124,58,237,0.15)',
            color: '#A78BFA',
          },
        },
      },
    },
    MuiDivider: {
      styleOverrides: {
        root: {
          borderColor: 'rgba(255,255,255,0.10)',
        },
      },
    },
    MuiTooltip: {
      styleOverrides: {
        tooltip: {
          backgroundColor: 'rgba(13,11,43,0.95)',
          border: '1px solid rgba(255,255,255,0.15)',
          backdropFilter: 'blur(12px)',
          fontSize: '0.8125rem',
        },
      },
    },
    MuiLinearProgress: {
      styleOverrides: {
        root: {
          backgroundColor: 'rgba(255,255,255,0.08)',
          borderRadius: '4px',
        },
        bar: {
          background: 'linear-gradient(90deg, #7C3AED, #2563EB)',
          borderRadius: '4px',
        },
      },
    },
    MuiCircularProgress: {
      styleOverrides: {
        root: {
          color: '#7C3AED',
        },
      },
    },
    MuiAvatar: {
      styleOverrides: {
        root: {
          background: 'linear-gradient(135deg, #7C3AED, #2563EB)',
          fontWeight: 700,
        },
      },
    },
  },
});
