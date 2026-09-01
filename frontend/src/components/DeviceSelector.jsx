export default function DeviceSelector({ label, devices, value, onChange, disabled, emptyLabel }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled || devices.length === 0}>
        {devices.length === 0 && <option value="">{emptyLabel}</option>}
        {devices.map((device, index) => (
          <option key={device.deviceId || index} value={device.deviceId}>
            {device.label || `${label} ${index + 1}`}
          </option>
        ))}
      </select>
    </label>
  );
}
