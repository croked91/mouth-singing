import { Chip } from '@mui/material';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';

interface Props {
  lyricsSource: string | null | undefined;
}

/**
 * Shown when the track's lyrics came from the raw Whisper ASR fallback
 * (no candidate from providers/agent passed the matcher). The text may
 * contain transcription errors — surface that to the singer.
 */
export default function LyricsAccuracyBadge({ lyricsSource }: Props) {
  if (lyricsSource !== 'asr_fallback') {
    return null;
  }

  return (
    <Chip
      icon={<WarningAmberIcon sx={{ fontSize: 14, color: 'inherit !important' }} />}
      label="Неточный текст"
      size="small"
      sx={{
        height: 24,
        fontSize: '12px',
        fontWeight: 600,
        letterSpacing: '0.02em',
        backgroundColor: 'rgba(248,113,113,0.18)',
        border: '1px solid rgba(248,113,113,0.4)',
        color: '#FCA5A5',
        borderRadius: '12px',
        '& .MuiChip-icon': {
          marginLeft: '6px',
          marginRight: '-2px',
          color: '#FCA5A5',
        },
        '& .MuiChip-label': {
          paddingLeft: '6px',
          paddingRight: '10px',
        },
      }}
    />
  );
}
