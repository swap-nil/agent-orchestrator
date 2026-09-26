{{- define "edge.g" -}}{{- toJson (.Values.global | default dict) -}}{{- end -}}

{{- define "edge.authority" -}}
{{- trimSuffix "/" ((.Values.global | default dict).authorityHost | default "https://login.microsoftonline.com") -}}
{{- end -}}

{{- define "edge.podSecurity" -}}
securityContext: {runAsNonRoot: true, runAsUser: {{ . }}, seccompProfile: {type: RuntimeDefault}}
{{- end -}}

{{- define "edge.containerSecurity" -}}
securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}
{{- end -}}

{{/* SecretProviderClass syncing Key Vault secrets into a Kubernetes Secret. Args: dict root name clientId secrets */}}
{{- define "edge.spc" -}}
{{- $g := .root.Values.global | default dict -}}
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: {{ .name }}-kv
spec:
  provider: azure
  parameters:
    clientID: {{ .clientId | quote }}
    keyvaultName: {{ required "global.keyVaultName is required" $g.keyVaultName | quote }}
    tenantId: {{ required "global.tenantId is required" $g.tenantId | quote }}
    objects: |
      array:
{{- range $kv, $env := .secrets }}
        - |
          objectName: {{ $kv }}
          objectType: secret
{{- end }}
  secretObjects:
    - secretName: {{ .name }}-secrets
      type: Opaque
      data:
{{- range $kv, $env := .secrets }}
        - {objectName: {{ $kv }}, key: {{ $kv }}}
{{- end }}
{{- end -}}

{{- define "edge.secretEnv" -}}
{{- $name := .name -}}
{{- range $kv, $env := .secrets }}
- name: {{ $env }}
  valueFrom: {secretKeyRef: {name: {{ $name }}-secrets, key: {{ $kv }}}}
{{- end }}
{{- end -}}
