// Copyright The Perses Authors
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import React, { createContext, ReactElement, useCallback, useContext, useEffect, useMemo, useState, useRef } from 'react';
import {
  AbsoluteTimeRange,
  DurationString,
  TimeRangeValue,
  isRelativeTimeRange,
  toAbsoluteTimeRange,
  getSuggestedStepMs,
} from '@perses-dev/spec';
import { useQueryClient } from '@tanstack/react-query';
import { getRefreshIntervalInMs } from './refresh-interval';

export interface TimeRangeProviderProps {
  timeRange: TimeRangeValue;
  refreshInterval?: DurationString;
  setTimeRange: (value: TimeRangeValue) => void;
  setRefreshInterval: (value: DurationString) => void;
  children?: React.ReactNode;
}

export interface TimeRange {
  timeRange: TimeRangeValue;
  absoluteTimeRange: AbsoluteTimeRange; // resolved absolute time for plugins to use
  setTimeRange: (value: TimeRangeValue) => void;
  refresh: () => void;
  refreshInterval?: DurationString;
  refreshIntervalInMs: number;
  setRefreshInterval: (value: DurationString) => void;
}

export const TimeRangeContext = createContext<TimeRange | undefined>(undefined);

export function useTimeRangeContext(): TimeRange {
  const ctx = useContext(TimeRangeContext);
  if (ctx === undefined) {
    throw new Error('No TimeRangeContext found. Did you forget a Provider?');
  }
  return ctx;
}

/**
 * Get and set the current resolved time range at runtime.
 */
export function useTimeRange(): TimeRange {
  return useTimeRangeContext();
}

/**
 * Gets the suggested step for a graph query in ms for the currently selected time range.
 */
export function useSuggestedStepMs(width?: number): number {
  const { absoluteTimeRange } = useTimeRange();
  if (width === undefined) return 0;
  return getSuggestedStepMs(absoluteTimeRange, width);
}

/**
 * Provider implementation that supplies the time range state at runtime.
 */
export function TimeRangeProvider(props: TimeRangeProviderProps): ReactElement {
  const { timeRange, refreshInterval, children, setTimeRange, setRefreshInterval } = props;

  const queryClient = useQueryClient();
  const [absoluteTimeRange, setAbsoluteTimeRange] = useState<AbsoluteTimeRange>(
    isRelativeTimeRange(timeRange) ? toAbsoluteTimeRange(timeRange) : timeRange
  );

  const handleSetTimeRange = useCallback(
    (value: TimeRangeValue) => {
      setTimeRange(value);
      setAbsoluteTimeRange(isRelativeTimeRange(value) ? toAbsoluteTimeRange(value) : value);
    },
    [setTimeRange]
  );

  const currentRange = useRef(absoluteTimeRange);
  currentRange.current = absoluteTimeRange;

  // Changing the range already changes every panel query key. Invalidating here
  // would refetch the old range before React commits the new one.
  const refreshPanels = useCallback(() => {
    const next = isRelativeTimeRange(timeRange) ? toAbsoluteTimeRange(timeRange) : timeRange;
    const previous = currentRange.current;
    if (+next.start !== +previous.start || +next.end !== +previous.end) {
      currentRange.current = next;
      setAbsoluteTimeRange(next);
    } else {
      void queryClient.invalidateQueries({ queryKey: ['query'] });
    }
  }, [queryClient, timeRange]);

  // Run after observers have switched keys; never remove active queries.
  useEffect(() => {
    const cleanup = setTimeout(() => {
      queryClient.removeQueries({ queryKey: ['query'], type: 'inactive' });
      queryClient.removeQueries({ queryKey: ['variable'], type: 'inactive' });
    }, 0);
    return () => clearTimeout(cleanup);
  }, [absoluteTimeRange, queryClient]);

  const refresh = useCallback(() => {
    refreshPanels();
    void queryClient.invalidateQueries({ queryKey: ['variable'] });
  }, [queryClient, refreshPanels]);
  const autoRefresh = refreshPanels;

  const refreshIntervalInMs = useMemo(() => getRefreshIntervalInMs(refreshInterval), [refreshInterval]);
  useEffect(() => {
    if (refreshIntervalInMs > 0) {
      const interval = setInterval(() => {
        autoRefresh();
      }, refreshIntervalInMs);

      return (): void => clearInterval(interval);
    }
  }, [autoRefresh, refreshIntervalInMs]);

  const ctx = useMemo(() => {
    return {
      timeRange: timeRange,
      setTimeRange: handleSetTimeRange,
      absoluteTimeRange: absoluteTimeRange,
      refresh,
      refreshInterval: refreshInterval,
      refreshIntervalInMs: refreshIntervalInMs,
      setRefreshInterval: setRefreshInterval,
    };
  }, [
    absoluteTimeRange,
    handleSetTimeRange,
    refresh,
    refreshInterval,
    refreshIntervalInMs,
    setRefreshInterval,
    timeRange,
  ]);

  return <TimeRangeContext.Provider value={ctx}>{children}</TimeRangeContext.Provider>;
}
