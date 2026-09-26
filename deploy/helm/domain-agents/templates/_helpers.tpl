{{- define "da.labels" -}}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: agent-orchestrator
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{- define "da.image" -}}
{{- $images := (.Values.global | default dict).images | default dict -}}
{{- if $images.mock -}}{{ $images.mock }}{{- else -}}{{ .Values.image.repository }}:{{ .Values.image.tag }}{{- end -}}
{{- end -}}

{{- define "da.authority" -}}
{{- trimSuffix "/" ((.Values.global | default dict).authorityHost | default "https://login.microsoftonline.com") -}}
{{- end -}}

{{- define "da.podSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: {{ . }}
  seccompProfile: {type: RuntimeDefault}
{{- end -}}

{{- define "da.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: {drop: ["ALL"]}
{{- end -}}
