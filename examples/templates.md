# Example message templates

Paste any of these into the **Message template** field of an inbox. Templates are
Jinja2; missing fields render as empty strings, nested fields use dots.

## Sales lead (`lead.json`)

```
🚀 New lead

Name: {{ name }}
Company: {{ company }}
Plan: {{ plan }}
Value: ${{ amount }}

{{ message }}
```

## Payment (`payment.json`)

```
💰 Payment {{ status }}

Customer: {{ customer }}
Amount: {{ amount | money }} {{ currency }}
Invoice: {{ invoice_id }}
```

## Production alert (`production-error.json`)

```
🚨 {{ level | upper }} in {{ service }} ({{ environment }})

{{ message }}
{{ error.type }} × {{ error.count_last_5m }} in the last 5 minutes

{{ url }}
```

## Signup (`signup.json`)

```
👋 New signup: {{ user.name }}

Email: {{ user.email }}
Plan: {{ plan }}
Referrer: {{ referrer }}
```

## Anything (fallback)

Leave the template empty, or use:

```
📬 {{ inbox.name }}

{{ payload | tojson_pretty }}
```
