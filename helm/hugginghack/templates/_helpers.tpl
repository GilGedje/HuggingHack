{{/*
Expand the name of the chart.
*/}}
{{- define "hugginghack.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "hugginghack.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "hugginghack.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "hugginghack.labels" -}}
helm.sh/chart: {{ include "hugginghack.chart" . }}
{{ include "hugginghack.selectorLabels" . }}
app.kubernetes.io/version: {{ (.Values.image.tag | default .Chart.AppVersion) | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: server
{{- with .Values.partOf }}
app.kubernetes.io/part-of: {{ . | quote }}
{{- end }}
{{- end }}

{{/*
Selector labels. They never change between upgrades, so they cannot include the version.
*/}}
{{- define "hugginghack.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hugginghack.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
The ServiceAccount the pods run as. The chart creates none; an existing one can be named,
otherwise the namespace's "default".
*/}}
{{- define "hugginghack.serviceAccountName" -}}
{{- (.Values.serviceAccount).name | default "default" }}
{{- end }}

{{/*
The in-chart PostgreSQL server (postgresql.enabled): its name, and labels that never match
the application's selector, so the application Service cannot reach the database pod.
*/}}
{{- define "hugginghack.postgresql.fullname" -}}
{{- printf "%s-postgresql" (include "hugginghack.fullname" . | trunc 52 | trimSuffix "-") }}
{{- end }}

{{- define "hugginghack.postgresql.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hugginghack.name" . }}-postgresql
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "hugginghack.postgresql.labels" -}}
helm.sh/chart: {{ include "hugginghack.chart" . }}
{{ include "hugginghack.postgresql.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.postgresql.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: database
{{- with .Values.partOf }}
app.kubernetes.io/part-of: {{ . | quote }}
{{- end }}
{{- end }}

{{/*
The PostgreSQL pod's security context. The official image starts as root, which "runAsNonRoot"
refuses, so outside OpenShift the image's own postgres user (70) is filled in, with its group
owning the volume. OpenShift (detected by its security API) assigns these from the
namespace's range and must not be given fixed ones. Values set explicitly always win.
*/}}
{{- define "hugginghack.postgresql.podSecurityContext" -}}
{{- $context := deepCopy (.Values.postgresql.podSecurityContext | default dict) }}
{{- if not (.Capabilities.APIVersions.Has "security.openshift.io/v1") }}
{{- range $key := list "runAsUser" "runAsGroup" "fsGroup" }}
{{- if not (hasKey $context $key) }}
{{- $_ := set $context $key 70 }}
{{- end }}
{{- end }}
{{- end }}
{{- toYaml $context }}
{{- end }}
