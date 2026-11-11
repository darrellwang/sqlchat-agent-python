{{/*
Expand the name of the chart.
*/}}
{{- define "mysqlagent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "mysqlagent.labels" -}}
app.kubernetes.io/name: {{ include "mysqlagent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "mysqlagent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mysqlagent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
API_KEYS 环境变量值："key1:name1:key1+key2,key2"
（config 解析器支持三段：key:name:tables，表清单用 + 连接）。
*/}}
{{- define "mysqlagent.apiKeysEnv" -}}
{{- range $i, $k := .Values.server.apiKeys -}}
{{- if $i }},{{ end -}}
{{- $k.key -}}
{{- if $k.name -}}:{{ $k.name }}{{ end -}}
{{- if $k.tables -}}:{{ join "+" $k.tables }}{{ end -}}
{{- end -}}
{{- end }}
