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

import { Alert, Button, Box, BoxProps } from '@mui/material';
import { BuiltinVariableDefinition } from '@perses-dev/spec';
import { ErrorBoundary, ErrorAlert, combineSx } from '@perses-dev/components';
import {
  TimeRangeProviderWithQueryParams,
  useInitialRefreshInterval,
  useInitialTimeRange,
  usePluginBuiltinVariableDefinitions,
  retryFailedPluginQueries,
} from '@perses-dev/plugin-system';
import { ReactElement, useMemo } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { DEFAULT_DASHBOARD_DURATION, DEFAULT_REFRESH_INTERVAL } from '../../constants';
import {
  DatasourceStoreProviderProps,
  DatasourceStoreProvider,
  VariableProviderProps,
  VariableProviderWithQueryParams,
  AnnotationProvider,
} from '../../context';
import { DashboardProviderWithQueryParams } from '../../context/DashboardProvider/DashboardProviderWithQueryParams';
import { DashboardApp, DashboardAppProps } from './DashboardApp';

export interface ViewDashboardProps extends Omit<BoxProps, 'children'>, DashboardAppProps {
  datasourceApi: DatasourceStoreProviderProps['datasourceApi'];
  externalVariableDefinitions?: VariableProviderProps['externalVariableDefinitions'];
  isEditing?: boolean;
  isCreating?: boolean;
}

/**
 * The View for displaying a Dashboard, along with the UI for selecting variable values.
 */
export function ViewDashboard(props: ViewDashboardProps): ReactElement {
  const {
    dashboardResource,
    datasourceApi,
    externalVariableDefinitions,
    emptyDashboardProps,
    isReadonly,
    isVariableEnabled,
    isAnnotationEnabled,
    isDatasourceEnabled,
    disableShortcuts,
    isEditing,
    isCreating,
    isInitialVariableSticky,
    isLeavingConfirmDialogEnabled,
    dashboardTitleComponent,
    onSave,
    onDiscard,
    sx,
    userPreferenceTimezone,
    ...others
  } = props;
  const queryClient = useQueryClient();
  const { spec } = dashboardResource;
  const dashboardDuration = spec.duration ?? DEFAULT_DASHBOARD_DURATION;
  const dashboardRefreshInterval = spec.refreshInterval ?? DEFAULT_REFRESH_INTERVAL;
  const initialTimeRange = useInitialTimeRange(dashboardDuration);
  const initialRefreshInterval = useInitialRefreshInterval(dashboardRefreshInterval);
  const requiredPluginNames = useMemo(() => {
    if (isEditing || isCreating) return undefined; // Full catalog is available when editing.
    const names = new Set<string>();
    const visit = (value: unknown): void => {
      if (!value || typeof value !== 'object') return;
      const object = value as Record<string, unknown>;
      const plugin = object.plugin as { kind?: string } | undefined;
      if (plugin?.kind) names.add(plugin.kind);
      Object.values(object).forEach(visit);
    };
    visit(spec);
    visit(externalVariableDefinitions);
    return [...names].sort();
  }, [spec, externalVariableDefinitions, isEditing, isCreating]);
  const { data, error, refetch, isFetching } = usePluginBuiltinVariableDefinitions(requiredPluginNames);

  const builtinVariables = useMemo(() => {
    const result = [
      {
        kind: 'BuiltinVariable',
        spec: {
          name: '__dashboard',
          value: () => dashboardResource.metadata.name,
          source: 'Dashboard',
          display: {
            name: '__dashboard',
            description: 'The name of the current dashboard',
            hidden: true,
          },
        },
      } as BuiltinVariableDefinition,
      {
        kind: 'BuiltinVariable',
        spec: {
          name: '__project',
          value: () => dashboardResource.metadata.project,
          source: 'Dashboard',
          display: {
            name: '__project',
            description: 'The name of the current dashboard project',
            hidden: true,
          },
        },
      } as BuiltinVariableDefinition,
    ];
    if (data) {
      data.forEach((def: BuiltinVariableDefinition) => result.push(def));
    }
    return result;
  }, [dashboardResource.metadata.name, dashboardResource.metadata.project, data]);

  return (
    <DatasourceStoreProvider dashboardResource={dashboardResource} datasourceApi={datasourceApi}>
      <DashboardProviderWithQueryParams
        initialState={{
          isEditMode: !!isEditing,
          dashboardResource,
        }}
      >
        <TimeRangeProviderWithQueryParams
          initialTimeRange={initialTimeRange}
          initialRefreshInterval={initialRefreshInterval}
        >
          <VariableProviderWithQueryParams
            initialVariableDefinitions={spec.variables}
            externalVariableDefinitions={externalVariableDefinitions}
            builtinVariableDefinitions={builtinVariables}
          >
            <AnnotationProvider initialAnnotationSpecs={spec.annotations ?? []}>
              <Box
                sx={combineSx(
                  {
                    display: 'flex',
                    width: '100%',
                    height: '100%',
                    position: 'relative',
                    overflow: 'hidden',
                  },
                  sx
                )}
                {...others}
              >
                {error ? <Alert severity="error" action={<Button disabled={isFetching} onClick={() => {
                  void retryFailedPluginQueries(queryClient);
                  void refetch();
                }}>Retry</Button>}>
                  Unable to load required monitoring plugins.
                </Alert> : null}
                <ErrorBoundary FallbackComponent={ErrorAlert}>
                  <DashboardApp
                    dashboardResource={dashboardResource}
                    emptyDashboardProps={emptyDashboardProps}
                    isReadonly={isReadonly}
                    isVariableEnabled={isVariableEnabled}
                    isAnnotationEnabled={isAnnotationEnabled}
                    isDatasourceEnabled={isDatasourceEnabled}
                    disableShortcuts={disableShortcuts}
                    isCreating={isCreating}
                    isInitialVariableSticky={isInitialVariableSticky}
                    isLeavingConfirmDialogEnabled={isLeavingConfirmDialogEnabled}
                    dashboardTitleComponent={dashboardTitleComponent}
                    onSave={onSave}
                    onDiscard={onDiscard}
                    userPreferenceTimezone={userPreferenceTimezone}
                  />
                </ErrorBoundary>
              </Box>
            </AnnotationProvider>
          </VariableProviderWithQueryParams>
        </TimeRangeProviderWithQueryParams>
      </DashboardProviderWithQueryParams>
    </DatasourceStoreProvider>
  );
}
