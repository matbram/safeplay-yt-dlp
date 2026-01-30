# SafePlay Website Integration for AI Agent Alerts

This document describes the changes needed to integrate the SafePlay AI Agent's alert system with the SafePlay website admin dashboard.

## Overview

The AI Agent sends alerts to the website when:
- Downloads fail after all retries
- The agent applies a fix
- The agent's fix fails
- The agent needs human intervention (escalation)
- System health degrades
- yt-dlp updates are available or applied

## Required Changes

### 1. Database Schema (Supabase)

Add this table to your Supabase database:

```sql
-- Admin alerts from the yt-dlp AI agent
CREATE TABLE admin_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),

    -- Classification
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error', 'critical')),
    alert_type TEXT NOT NULL CHECK (alert_type IN (
        'download_failure',
        'fix_applied',
        'fix_failed',
        'escalation',
        'pattern_anomaly',
        'health_degraded',
        'ytdlp_update_available',
        'ytdlp_updated',
        'system_maintenance'
    )),

    -- Content
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    details JSONB DEFAULT '{}',

    -- Related entities
    job_id TEXT,
    youtube_id TEXT,

    -- Status tracking
    acknowledged BOOLEAN DEFAULT FALSE,
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by UUID REFERENCES auth.users(id),
    resolved BOOLEAN DEFAULT FALSE,
    resolved_at TIMESTAMPTZ,

    -- Source tracking
    source TEXT DEFAULT 'ytdlp_agent'
);

-- Indexes
CREATE INDEX idx_admin_alerts_created_at ON admin_alerts(created_at DESC);
CREATE INDEX idx_admin_alerts_severity ON admin_alerts(severity);
CREATE INDEX idx_admin_alerts_acknowledged ON admin_alerts(acknowledged);
CREATE INDEX idx_admin_alerts_type ON admin_alerts(alert_type);
```

### 2. API Webhook Endpoint

Create a new file: `src/app/api/webhooks/ytdlp-alerts/route.ts`

```typescript
import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

// Use service role for inserting alerts
const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!
);

// Verify the request is from the agent
function verifyApiKey(request: NextRequest): boolean {
  const apiKey = request.headers.get('X-API-Key');
  return apiKey === process.env.YTDLP_AGENT_API_KEY;
}

export async function POST(request: NextRequest) {
  // Verify authentication
  if (!verifyApiKey(request)) {
    return NextResponse.json(
      { error: 'Unauthorized' },
      { status: 401 }
    );
  }

  try {
    const payload = await request.json();

    // Validate required fields
    if (!payload.severity || !payload.alert_type || !payload.title || !payload.message) {
      return NextResponse.json(
        { error: 'Missing required fields' },
        { status: 400 }
      );
    }

    // Insert the alert
    const { data, error } = await supabase
      .from('admin_alerts')
      .insert({
        severity: payload.severity,
        alert_type: payload.alert_type,
        title: payload.title,
        message: payload.message,
        details: payload.details || {},
        job_id: payload.job_id,
        youtube_id: payload.youtube_id,
        source: 'ytdlp_agent',
      })
      .select()
      .single();

    if (error) {
      console.error('Failed to insert alert:', error);
      return NextResponse.json(
        { error: 'Failed to save alert' },
        { status: 500 }
      );
    }

    return NextResponse.json({
      success: true,
      alert_id: data.id,
    });

  } catch (error) {
    console.error('Webhook error:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
```

### 3. Environment Variable

Add to your `.env.local`:

```bash
# API key for yt-dlp agent webhook authentication
YTDLP_AGENT_API_KEY=your-secure-api-key-here
```

### 4. Admin Dashboard Alerts Page

Create a new file: `src/app/(admin)/admin/alerts/page.tsx`

```typescript
'use client';

import { useEffect, useState } from 'react';
import { createClientComponentClient } from '@supabase/auth-helpers-nextjs';
import { Badge } from '@/components/ui/badge';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';

interface Alert {
  id: string;
  created_at: string;
  severity: 'info' | 'warning' | 'error' | 'critical';
  alert_type: string;
  title: string;
  message: string;
  details: any;
  job_id?: string;
  youtube_id?: string;
  acknowledged: boolean;
  resolved: boolean;
}

const severityColors = {
  info: 'bg-blue-100 text-blue-800',
  warning: 'bg-yellow-100 text-yellow-800',
  error: 'bg-red-100 text-red-800',
  critical: 'bg-red-200 text-red-900 font-bold',
};

const severityEmoji = {
  info: 'ℹ️',
  warning: '⚠️',
  error: '❌',
  critical: '🚨',
};

export default function AlertsPage() {
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<'all' | 'unacknowledged'>('unacknowledged');
  const supabase = createClientComponentClient();

  useEffect(() => {
    fetchAlerts();

    // Subscribe to new alerts
    const channel = supabase
      .channel('admin_alerts')
      .on('postgres_changes', {
        event: 'INSERT',
        schema: 'public',
        table: 'admin_alerts',
      }, (payload) => {
        setAlerts((prev) => [payload.new as Alert, ...prev]);
      })
      .subscribe();

    return () => {
      supabase.removeChannel(channel);
    };
  }, [filter]);

  async function fetchAlerts() {
    setLoading(true);
    let query = supabase
      .from('admin_alerts')
      .select('*')
      .order('created_at', { ascending: false })
      .limit(100);

    if (filter === 'unacknowledged') {
      query = query.eq('acknowledged', false);
    }

    const { data, error } = await query;

    if (error) {
      console.error('Failed to fetch alerts:', error);
    } else {
      setAlerts(data || []);
    }
    setLoading(false);
  }

  async function acknowledgeAlert(alertId: string) {
    const { error } = await supabase
      .from('admin_alerts')
      .update({
        acknowledged: true,
        acknowledged_at: new Date().toISOString(),
      })
      .eq('id', alertId);

    if (!error) {
      setAlerts((prev) =>
        prev.map((a) =>
          a.id === alertId ? { ...a, acknowledged: true } : a
        )
      );
    }
  }

  async function acknowledgeAll() {
    const unacknowledgedIds = alerts
      .filter((a) => !a.acknowledged)
      .map((a) => a.id);

    if (unacknowledgedIds.length === 0) return;

    const { error } = await supabase
      .from('admin_alerts')
      .update({
        acknowledged: true,
        acknowledged_at: new Date().toISOString(),
      })
      .in('id', unacknowledgedIds);

    if (!error) {
      setAlerts((prev) =>
        prev.map((a) => ({ ...a, acknowledged: true }))
      );
    }
  }

  const unacknowledgedCount = alerts.filter((a) => !a.acknowledged).length;

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold">System Alerts</h1>
          <p className="text-gray-500">
            Alerts from the YouTube download AI agent
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant={filter === 'all' ? 'default' : 'outline'}
            onClick={() => setFilter('all')}
          >
            All
          </Button>
          <Button
            variant={filter === 'unacknowledged' ? 'default' : 'outline'}
            onClick={() => setFilter('unacknowledged')}
          >
            Unacknowledged ({unacknowledgedCount})
          </Button>
          {unacknowledgedCount > 0 && (
            <Button variant="outline" onClick={acknowledgeAll}>
              Acknowledge All
            </Button>
          )}
        </div>
      </div>

      {loading ? (
        <div className="text-center py-8">Loading...</div>
      ) : alerts.length === 0 ? (
        <Card>
          <CardContent className="py-8 text-center text-gray-500">
            No alerts to display
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          {alerts.map((alert) => (
            <Card
              key={alert.id}
              className={alert.acknowledged ? 'opacity-60' : ''}
            >
              <CardHeader className="pb-2">
                <div className="flex justify-between items-start">
                  <div className="flex items-center gap-2">
                    <span>{severityEmoji[alert.severity]}</span>
                    <CardTitle className="text-lg">{alert.title}</CardTitle>
                    <Badge className={severityColors[alert.severity]}>
                      {alert.severity}
                    </Badge>
                    <Badge variant="outline">{alert.alert_type}</Badge>
                  </div>
                  {!alert.acknowledged && (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => acknowledgeAlert(alert.id)}
                    >
                      Acknowledge
                    </Button>
                  )}
                </div>
              </CardHeader>
              <CardContent>
                <p className="text-gray-700 mb-2">{alert.message}</p>
                <div className="flex gap-4 text-sm text-gray-500">
                  <span>
                    {new Date(alert.created_at).toLocaleString()}
                  </span>
                  {alert.job_id && <span>Job: {alert.job_id}</span>}
                  {alert.youtube_id && (
                    <span>Video: {alert.youtube_id}</span>
                  )}
                </div>
                {alert.details && Object.keys(alert.details).length > 0 && (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-sm text-gray-500">
                      Details
                    </summary>
                    <pre className="mt-2 p-2 bg-gray-100 rounded text-xs overflow-auto">
                      {JSON.stringify(alert.details, null, 2)}
                    </pre>
                  </details>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
```

### 5. Update Admin Navigation

In `src/app/(admin)/layout.tsx`, add the alerts link to the sidebar:

```typescript
// Add to the navigation items array:
{
  name: 'Alerts',
  href: '/admin/alerts',
  icon: BellAlertIcon,  // from @heroicons/react
}
```

### 6. Notification Badge in Header

Update the bell icon in the admin header to show unacknowledged alert count:

```typescript
// In the header component, add a query for unacknowledged alerts:
const { data: alertCount } = useQuery({
  queryKey: ['unacknowledged-alerts'],
  queryFn: async () => {
    const { count } = await supabase
      .from('admin_alerts')
      .select('*', { count: 'exact', head: true })
      .eq('acknowledged', false);
    return count || 0;
  },
  refetchInterval: 30000, // Refresh every 30 seconds
});

// Then in the JSX:
<Link href="/admin/alerts" className="relative">
  <BellIcon className="h-6 w-6" />
  {alertCount > 0 && (
    <span className="absolute -top-1 -right-1 bg-red-500 text-white text-xs rounded-full h-5 w-5 flex items-center justify-center">
      {alertCount > 9 ? '9+' : alertCount}
    </span>
  )}
</Link>
```

## Configuration on Agent Side

After implementing the website changes, configure the agent's `.env`:

```bash
SAFEPLAY_WEBSITE_URL=https://your-safeplay-website.com
SAFEPLAY_WEBSITE_API_KEY=your-secure-api-key-here
```

The API key should match what you set in `YTDLP_AGENT_API_KEY` on the website.

## Alert Types

| Type | Description |
|------|-------------|
| `download_failure` | Download failed after all retries |
| `fix_applied` | Agent applied a fix successfully |
| `fix_failed` | Agent's fix didn't resolve the issue |
| `escalation` | Agent needs human intervention |
| `pattern_anomaly` | Unusual pattern detected (e.g., spike in failures) |
| `health_degraded` | Overall download success rate dropped |
| `ytdlp_update_available` | New yt-dlp version available |
| `ytdlp_updated` | yt-dlp was updated |
| `system_maintenance` | Agent performed maintenance (startup, etc.) |

## Severity Levels

| Level | When Used |
|-------|-----------|
| `info` | Informational (fix applied, yt-dlp updated) |
| `warning` | Attention needed but not urgent (health degraded) |
| `error` | Problem occurred (download failed, fix failed) |
| `critical` | Immediate attention required (escalation) |

## Testing the Integration

1. Deploy the website changes
2. Set the environment variables on both sides
3. Start the AI agent
4. The agent will send a "system_maintenance" alert on startup
5. Check the admin dashboard to verify the alert appears

## Real-time Updates

The alerts page uses Supabase real-time subscriptions to show new alerts immediately without refreshing. Make sure real-time is enabled for the `admin_alerts` table in your Supabase project settings.
