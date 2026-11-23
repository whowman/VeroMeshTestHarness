{{- define "harness.name" -}}{{ .Release.Name }}-test-harness{{- end -}}
{{- define "harness.labels" -}}
app.kubernetes.io/name: veromesh-test-harness
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}
{{- define "harness.selector" -}}
app.kubernetes.io/name: veromesh-test-harness
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
