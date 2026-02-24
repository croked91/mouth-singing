import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { Box, IconButton, Typography, CircularProgress } from '@mui/material';
import LockIcon from '@mui/icons-material/Lock';
import CloseIcon from '@mui/icons-material/Close';
import BackspaceIcon from '@mui/icons-material/Backspace';
import CheckIcon from '@mui/icons-material/Check';
import PowerSettingsNewIcon from '@mui/icons-material/PowerSettingsNew';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import { api } from '../services/api';

// ─── Types ────────────────────────────────────────────────────────────────────

interface AdminModalProps {
  open: boolean;
  onClose: () => void;
  sessionId: string;
}

type ModalState = 'pin' | 'wrong' | 'unlocked' | 'confirm';

// ─── Constants ────────────────────────────────────────────────────────────────

const PIN_LENGTH = 4;
const WRONG_PIN_RESET_MS = 1500;

// Numpad layout: null = empty cell placeholder (not used), 'backspace' and 'confirm' are special
type NumpadKey = string | 'backspace' | 'confirm';

const NUMPAD_ROWS: NumpadKey[][] = [
  ['1', '2', '3'],
  ['4', '5', '6'],
  ['7', '8', '9'],
  ['backspace', '0', 'confirm'],
];

// ─── PIN Dot ──────────────────────────────────────────────────────────────────

interface PinDotProps {
  filled: boolean;
  active: boolean;
  wrong: boolean;
  correct: boolean;
  index: number;
}

const PinDot: React.FC<PinDotProps> = ({ filled, active, wrong, correct, index }) => {
  const getBg = (): string => {
    if (correct) return '#10B981';
    if (wrong) return 'rgba(239,68,68,0.2)';
    if (filled) return 'linear-gradient(135deg, #7C3AED, #2563EB)';
    return 'rgba(255,255,255,0.06)';
  };

  const getBorder = (): string => {
    if (correct) return '2px solid #10B981';
    if (wrong) return '2px solid rgba(239,68,68,0.6)';
    if (filled) return '2px solid #A78BFA';
    if (active) return '2px solid #A78BFA';
    return '2px solid rgba(255,255,255,0.15)';
  };

  const getBoxShadow = (): string => {
    if (correct) return '0 0 12px rgba(16,185,129,0.4)';
    if (wrong) return 'none';
    if (filled) return '0 0 12px rgba(124,58,237,0.5)';
    if (active) return '0 0 0 3px rgba(167,139,250,0.2)';
    return 'none';
  };

  return (
    <Box
      sx={{
        width: 52,
        height: 52,
        borderRadius: '50%',
        background: getBg(),
        border: getBorder(),
        boxShadow: getBoxShadow(),
        transition: 'all 0.2s ease',
        // Pulse animation for the currently active (empty) slot
        ...(active && !filled && !wrong && !correct
          ? {
              '@keyframes dotPulse': {
                '0%, 100%': { boxShadow: '0 0 0 0 rgba(167,139,250,0.35)' },
                '50%': { boxShadow: '0 0 0 6px rgba(167,139,250,0)' },
              },
              animation: `dotPulse 1.6s ease-in-out ${index * 0.1}s infinite`,
            }
          : {}),
      }}
    />
  );
};

// ─── AdminModal ───────────────────────────────────────────────────────────────

export const AdminModal: React.FC<AdminModalProps> = ({ open, onClose, sessionId }) => {
  const navigate = useNavigate();

  const [modalState, setModalState] = useState<ModalState>('pin');
  const [pin, setPin] = useState<string>('');
  const [storedPin, setStoredPin] = useState<string>('');
  const [shaking, setShaking] = useState(false);
  const [isTerminating, setIsTerminating] = useState(false);

  const wrongResetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Reset state when modal closes/reopens
  useEffect(() => {
    if (open) {
      setModalState('pin');
      setPin('');
      setStoredPin('');
      setShaking(false);
      setIsTerminating(false);
    }
    return () => {
      if (wrongResetTimerRef.current !== null) {
        clearTimeout(wrongResetTimerRef.current);
      }
    };
  }, [open]);

  // ── Handlers ──────────────────────────────────────────────────────────────

  const handleDigit = useCallback(
    (digit: string): void => {
      if (modalState !== 'pin') return;
      if (pin.length >= PIN_LENGTH) return;
      setPin((prev) => prev + digit);
    },
    [modalState, pin.length]
  );

  const handleBackspace = useCallback((): void => {
    if (modalState !== 'pin') return;
    setPin((prev) => prev.slice(0, -1));
  }, [modalState]);

  const handleConfirmPin = useCallback((): void => {
    if (pin.length < PIN_LENGTH) return;
    // Store PIN and move to unlocked state (validation deferred to terminate call)
    setStoredPin(pin);
    setModalState('unlocked');
    setPin('');
  }, [pin]);

  const handleNumpadKey = useCallback(
    (key: NumpadKey): void => {
      if (key === 'backspace') {
        handleBackspace();
      } else if (key === 'confirm') {
        handleConfirmPin();
      } else {
        handleDigit(key);
      }
    },
    [handleBackspace, handleConfirmPin, handleDigit]
  );

  const handleTerminateClick = useCallback((): void => {
    setModalState('confirm');
  }, []);

  const handleCancelConfirm = useCallback((): void => {
    setModalState('unlocked');
  }, []);

  const handleCancelUnlocked = useCallback((): void => {
    onClose();
  }, [onClose]);

  const handleConfirmTerminate = useCallback(async (): Promise<void> => {
    setIsTerminating(true);
    try {
      await api.terminateSession(sessionId, storedPin);
      navigate('/');
    } catch (err) {
      // 403 = wrong PIN — show State B then reset
      setIsTerminating(false);
      setModalState('wrong');
      setStoredPin('');
      setShaking(true);

      wrongResetTimerRef.current = setTimeout(() => {
        setModalState('pin');
        setPin('');
        setShaking(false);
      }, WRONG_PIN_RESET_MS);
    }
  }, [sessionId, storedPin, navigate]);

  if (!open) return null;

  // ── Derived values ──────────────────────────────────────────────────────

  const isPinState = modalState === 'pin';
  const isWrongState = modalState === 'wrong';
  const isUnlockedState = modalState === 'unlocked';
  const isConfirmState = modalState === 'confirm';
  const isCorrectState = isUnlockedState || isConfirmState;

  // How many dots to show as filled: in pin state use current input length,
  // in wrong state show all 4 as wrong, in unlocked/confirm show all 4 as correct
  const filledCount = isPinState ? pin.length : PIN_LENGTH;

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <Box
      role="dialog"
      aria-modal="true"
      aria-label="Панель администратора"
      onClick={(e) => {
        // Close on backdrop click
        if (e.target === e.currentTarget) onClose();
      }}
      sx={{
        position: 'fixed',
        inset: 0,
        zIndex: 1400,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        backgroundColor: 'rgba(5,5,8,0.7)',
        backdropFilter: 'blur(24px)',
        WebkitBackdropFilter: 'blur(24px)',
        p: 2,
      }}
    >
      {/* Modal card */}
      <Box
        onClick={(e) => e.stopPropagation()}
        sx={{
          width: 460,
          maxWidth: '100%',
          borderRadius: '28px',
          backgroundColor: 'rgba(20,15,45,0.92)',
          border: '1px solid rgba(255,255,255,0.12)',
          backdropFilter: 'blur(32px)',
          WebkitBackdropFilter: 'blur(32px)',
          boxShadow: '0 32px 80px rgba(0,0,0,0.6)',
          padding: '48px 40px',
          position: 'relative',
          // Shake animation for wrong PIN
          '@keyframes shake': {
            '0%, 100%': { transform: 'translateX(0)' },
            '10%, 50%, 90%': { transform: 'translateX(-8px)' },
            '30%, 70%': { transform: 'translateX(8px)' },
          },
          ...(shaking ? { animation: 'shake 0.4s ease' } : {}),
        }}
      >
        {/* Close button */}
        <IconButton
          aria-label="Закрыть"
          onClick={onClose}
          size="small"
          sx={{
            position: 'absolute',
            top: 12,
            right: 12,
            width: 36,
            height: 36,
            color: 'rgba(255,255,255,0.45)',
            '&:hover': {
              backgroundColor: 'rgba(255,255,255,0.1)',
              color: '#fff',
            },
          }}
        >
          <CloseIcon sx={{ fontSize: 18 }} />
        </IconButton>

        {/* ── Header ── */}
        <Box
          sx={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: 1.5,
            mb: 3.5,
          }}
        >
          {/* Lock icon circle */}
          <Box
            sx={{
              width: 64,
              height: 64,
              borderRadius: '50%',
              backgroundColor: 'rgba(124,58,237,0.15)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              border: '1px solid rgba(124,58,237,0.3)',
            }}
          >
            <LockIcon sx={{ fontSize: 28, color: '#A78BFA' }} />
          </Box>

          <Typography
            sx={{
              fontFamily: 'Inter, sans-serif',
              fontWeight: 700,
              fontSize: '26px',
              color: '#FFFFFF',
              textAlign: 'center',
              lineHeight: 1.2,
            }}
          >
            Доступ администратора
          </Typography>

          <Typography
            sx={{
              fontFamily: 'Inter, sans-serif',
              fontWeight: 400,
              fontSize: '15px',
              color: 'rgba(255,255,255,0.45)',
              textAlign: 'center',
            }}
          >
            {isPinState && 'Введите PIN для продолжения'}
            {isWrongState && 'Введите PIN для продолжения'}
            {isUnlockedState && 'Доступ открыт'}
            {isConfirmState && 'Подтвердите действие'}
          </Typography>
        </Box>

        {/* ── PIN Dots ── */}
        <Box
          sx={{
            display: 'flex',
            justifyContent: 'center',
            gap: '16px',
            mb: 3,
          }}
        >
          {Array.from({ length: PIN_LENGTH }, (_, i) => (
            <PinDot
              key={i}
              index={i}
              filled={i < filledCount}
              active={isPinState && i === pin.length}
              wrong={isWrongState}
              correct={isCorrectState}
            />
          ))}
        </Box>

        {/* ── Wrong PIN error text ── */}
        {isWrongState && (
          <Typography
            sx={{
              textAlign: 'center',
              color: '#F87171',
              fontSize: '14px',
              fontWeight: 500,
              mb: 2,
              mt: -1,
            }}
          >
            Неверный PIN
          </Typography>
        )}

        {/* ── PIN State: Numpad ── */}
        {(isPinState || isWrongState) && (
          <Box
            sx={{
              display: 'grid',
              gridTemplateColumns: 'repeat(3, 80px)',
              gridTemplateRows: 'repeat(4, 60px)',
              gap: '10px',
              justifyContent: 'center',
              mt: isWrongState ? 0 : 1,
            }}
          >
            {NUMPAD_ROWS.flat().map((key, idx) => {
              const isConfirmKey = key === 'confirm';
              const isBackspaceKey = key === 'backspace';
              const isDigit = !isConfirmKey && !isBackspaceKey;
              const confirmActive = isConfirmKey && pin.length === PIN_LENGTH;

              return (
                <Box
                  key={idx}
                  component="button"
                  disabled={isConfirmKey && !confirmActive}
                  onClick={() => { handleNumpadKey(key); }}
                  aria-label={
                    isBackspaceKey
                      ? 'Удалить'
                      : isConfirmKey
                      ? 'Подтвердить'
                      : key
                  }
                  sx={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    borderRadius: '12px',
                    cursor: isConfirmKey && !confirmActive ? 'not-allowed' : 'pointer',
                    border: 'none',
                    outline: 'none',
                    userSelect: 'none',
                    transition: 'all 0.15s ease',
                    // Digit keys
                    ...(isDigit && {
                      backgroundColor: 'rgba(255,255,255,0.07)',
                      color: '#FFFFFF',
                      fontSize: '24px',
                      fontWeight: 700,
                      fontFamily: 'Inter, sans-serif',
                      '&:hover': {
                        backgroundColor: 'rgba(255,255,255,0.14)',
                      },
                      '&:active': {
                        backgroundColor: 'rgba(124,58,237,0.3)',
                        transform: 'scale(0.96)',
                      },
                    }),
                    // Backspace key
                    ...(isBackspaceKey && {
                      backgroundColor: 'rgba(255,255,255,0.05)',
                      color: 'rgba(255,255,255,0.4)',
                      '&:hover': {
                        backgroundColor: 'rgba(255,255,255,0.10)',
                        color: 'rgba(255,255,255,0.7)',
                      },
                      '&:active': {
                        transform: 'scale(0.96)',
                      },
                    }),
                    // Confirm key
                    ...(isConfirmKey && {
                      backgroundColor: confirmActive
                        ? 'rgba(16,185,129,0.15)'
                        : 'rgba(255,255,255,0.03)',
                      color: confirmActive ? '#10B981' : 'rgba(255,255,255,0.2)',
                      border: confirmActive
                        ? '1px solid rgba(16,185,129,0.3)'
                        : '1px solid rgba(255,255,255,0.06)',
                      '&:hover': confirmActive
                        ? { backgroundColor: 'rgba(16,185,129,0.25)' }
                        : {},
                      '&:active': confirmActive
                        ? { transform: 'scale(0.96)' }
                        : {},
                    }),
                  }}
                >
                  {isBackspaceKey && <BackspaceIcon sx={{ fontSize: 22 }} />}
                  {isConfirmKey && <CheckIcon sx={{ fontSize: 22 }} />}
                  {isDigit && key}
                </Box>
              );
            })}
          </Box>
        )}

        {/* ── Unlocked State: Admin Actions ── */}
        {isUnlockedState && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
            <Typography
              sx={{
                fontFamily: 'Inter, sans-serif',
                fontWeight: 600,
                fontSize: '11px',
                letterSpacing: '0.12em',
                color: 'rgba(255,255,255,0.35)',
                textTransform: 'uppercase',
                mb: 0.5,
              }}
            >
              УПРАВЛЕНИЕ
            </Typography>

            {/* Terminate session button */}
            <Box
              component="button"
              onClick={handleTerminateClick}
              sx={{
                width: '100%',
                height: 56,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 1.5,
                borderRadius: '14px',
                backgroundColor: 'rgba(239,68,68,0.12)',
                border: '1px solid rgba(239,68,68,0.3)',
                color: '#F87171',
                fontSize: '14px',
                fontWeight: 700,
                fontFamily: 'Inter, sans-serif',
                letterSpacing: '0.08em',
                textTransform: 'uppercase',
                cursor: 'pointer',
                transition: 'all 0.2s ease',
                '&:hover': {
                  backgroundColor: 'rgba(239,68,68,0.2)',
                  borderColor: 'rgba(239,68,68,0.5)',
                },
                '&:active': {
                  transform: 'scale(0.98)',
                },
              }}
            >
              <PowerSettingsNewIcon sx={{ fontSize: 20 }} />
              ЗАВЕРШИТЬ СЕССИЮ
            </Box>

            {/* Cancel button */}
            <Box
              component="button"
              onClick={handleCancelUnlocked}
              sx={{
                width: '100%',
                height: 52,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                borderRadius: '14px',
                backgroundColor: 'rgba(255,255,255,0.05)',
                border: '1px solid rgba(255,255,255,0.08)',
                color: 'rgba(255,255,255,0.55)',
                fontSize: '14px',
                fontWeight: 600,
                fontFamily: 'Inter, sans-serif',
                cursor: 'pointer',
                transition: 'all 0.2s ease',
                '&:hover': {
                  backgroundColor: 'rgba(255,255,255,0.10)',
                  color: 'rgba(255,255,255,0.8)',
                },
              }}
            >
              Отмена
            </Box>
          </Box>
        )}

        {/* ── Confirm State: Confirmation card ── */}
        {isConfirmState && (
          <Box
            sx={{
              borderRadius: '16px',
              backgroundColor: 'rgba(239,68,68,0.08)',
              border: '1px solid rgba(239,68,68,0.25)',
              p: '20px',
            }}
          >
            {/* Warning icon */}
            <Box sx={{ display: 'flex', justifyContent: 'center', mb: 1.5 }}>
              <WarningAmberIcon sx={{ fontSize: 32, color: '#FBBF24' }} />
            </Box>

            {/* Title */}
            <Typography
              sx={{
                fontFamily: 'Inter, sans-serif',
                fontWeight: 700,
                fontSize: '18px',
                color: '#FFFFFF',
                textAlign: 'center',
                mb: 1,
              }}
            >
              Завершить сессию?
            </Typography>

            {/* Description */}
            <Typography
              sx={{
                fontFamily: 'Inter, sans-serif',
                fontWeight: 400,
                fontSize: '13px',
                color: 'rgba(255,255,255,0.55)',
                textAlign: 'center',
                lineHeight: 1.6,
                mb: 2.5,
              }}
            >
              Очередь будет очищена, приложение вернётся на стартовый экран. Это действие
              необратимо.
            </Typography>

            {/* Action buttons */}
            <Box sx={{ display: 'flex', gap: 1.5 }}>
              {/* Cancel */}
              <Box
                component="button"
                onClick={handleCancelConfirm}
                disabled={isTerminating}
                sx={{
                  flex: 1,
                  height: 44,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  borderRadius: '12px',
                  backgroundColor: 'rgba(255,255,255,0.06)',
                  border: '1px solid rgba(255,255,255,0.10)',
                  color: 'rgba(255,255,255,0.65)',
                  fontSize: '14px',
                  fontWeight: 600,
                  fontFamily: 'Inter, sans-serif',
                  cursor: isTerminating ? 'not-allowed' : 'pointer',
                  transition: 'all 0.15s ease',
                  opacity: isTerminating ? 0.5 : 1,
                  '&:hover': isTerminating
                    ? {}
                    : {
                        backgroundColor: 'rgba(255,255,255,0.12)',
                        color: '#fff',
                      },
                }}
              >
                Отмена
              </Box>

              {/* Confirm terminate */}
              <Box
                component="button"
                onClick={() => { void handleConfirmTerminate(); }}
                disabled={isTerminating}
                sx={{
                  flex: 1,
                  height: 44,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: 1,
                  borderRadius: '12px',
                  backgroundColor: '#DC2626',
                  border: '1px solid rgba(220,38,38,0.6)',
                  color: '#FFFFFF',
                  fontSize: '14px',
                  fontWeight: 700,
                  fontFamily: 'Inter, sans-serif',
                  cursor: isTerminating ? 'not-allowed' : 'pointer',
                  boxShadow: isTerminating ? 'none' : '0 0 16px rgba(220,38,38,0.4)',
                  transition: 'all 0.15s ease',
                  opacity: isTerminating ? 0.7 : 1,
                  '&:hover': isTerminating
                    ? {}
                    : {
                        backgroundColor: '#B91C1C',
                        boxShadow: '0 0 24px rgba(220,38,38,0.6)',
                      },
                  '&:active': isTerminating ? {} : { transform: 'scale(0.98)' },
                }}
              >
                {isTerminating ? (
                  <CircularProgress size={16} sx={{ color: '#fff' }} />
                ) : (
                  'Да, завершить'
                )}
              </Box>
            </Box>
          </Box>
        )}
      </Box>
    </Box>
  );
};
