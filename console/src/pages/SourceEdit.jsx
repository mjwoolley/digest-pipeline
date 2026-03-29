import { useState, useEffect, useRef } from 'preact/hooks';
import { fetchApi, mutateApi } from '../api';

const SOURCE_TYPES = [
  { value: 'twitter', label: 'Twitter' },
  { value: 'blog', label: 'Blog' },
  { value: 'research', label: 'Research' },
  { value: 'newsletter', label: 'Newsletter' },
  { value: 'github_trending', label: 'GitHub Trending' },
];

const SCRAPE_PARSERS = [
  { value: 'anthropic_news', label: 'anthropic_news' },
  { value: 'addepar_blog', label: 'addepar_blog' },
];

function autoKey(type, fields) {
  if (type === 'twitter') return fields.account || '';
  if (type === 'github_trending') return 'trending';
  return '';
}

function needsManualKey(type) {
  return type === 'blog' || type === 'research' || type === 'newsletter';
}

export function SourceEdit({ slug, sourceType, sourceKey, refreshInterval }) {
  const isCreate = !sourceType;

  const [selectedType, setSelectedType] = useState('twitter');
  const [fields, setFields] = useState({});
  const [manualKey, setManualKey] = useState('');
  const [sharedSettings, setSharedSettings] = useState(null);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const dialogRef = useRef(null);
  const [serverError, setServerError] = useState(null);
  const [loading, setLoading] = useState(!isCreate);
  const [hasGithubTrending, setHasGithubTrending] = useState(false);

  // Fetch existing source detail in edit mode
  useEffect(() => {
    if (!isCreate) {
      setLoading(true);
      fetchApi(`/api/digests/${slug}/sources/${sourceType}/${sourceKey}`)
        .then((data) => {
          setFields(data.fields || {});
          setSharedSettings(data.shared_settings || null);
          setLoading(false);
        })
        .catch((err) => {
          setServerError(err.message);
          setLoading(false);
        });
    }
  }, [slug, sourceType, sourceKey]);

  // In create mode, check which types already exist
  useEffect(() => {
    if (isCreate) {
      fetchApi(`/api/digests/${slug}/sources`)
        .then((sources) => {
          const hasGH = sources.some((s) => s.source_type === 'github_trending');
          setHasGithubTrending(hasGH);
        })
        .catch(() => {});
    }
  }, [slug]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (showDeleteDialog) {
      dialog.show();
    } else {
      dialog.close();
    }
  }, [showDeleteDialog]);

  const activeType = isCreate ? selectedType : sourceType;

  function setField(name, value) {
    setFields((prev) => ({ ...prev, [name]: value }));
    if (errors[name]) {
      setErrors((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
    }
  }

  function validate() {
    const errs = {};
    const t = activeType;

    if (t === 'twitter') {
      if (!fields.account?.trim()) errs.account = 'Required';
    }

    if (t === 'blog' || t === 'research') {
      if (!fields.name?.trim()) errs.name = 'Required';
      const strategy = fields.strategy || 'rss';
      if (strategy === 'rss' && !fields.feed_url?.trim()) errs.feed_url = 'Required';
      if (strategy === 'html_scrape') {
        if (!fields.url?.trim()) errs.url = 'Required';
        if (!fields.scrape_parser) errs.scrape_parser = 'Required';
      }
    }

    if (t === 'newsletter') {
      if (!fields.name?.trim()) errs.name = 'Required';
      if (!fields.from?.trim()) errs.from = 'Required';
    }

    if (t === 'github_trending') {
      if (!fields.name?.trim()) errs.name = 'Required';
      if (!fields.url?.trim()) errs.url = 'Required';
      if (!fields.min_stars_week && fields.min_stars_week !== 0) errs.min_stars_week = 'Required';
      if (!fields.ai_keywords?.trim()) errs.ai_keywords = 'Required';
    }

    if (isCreate && needsManualKey(t)) {
      if (!manualKey.trim()) {
        errs.key = 'Required';
      } else if (!/^[a-z0-9_]+$/.test(manualKey)) {
        errs.key = 'Lowercase alphanumeric and underscores only';
      }
    }

    setErrors(errs);
    return Object.keys(errs).length === 0;
  }

  function buildBody() {
    const t = activeType;
    const cleanFields = {};

    if (t === 'twitter') {
      // For twitter, fields are empty; key is the account handle
    }

    if (t === 'blog' || t === 'research') {
      if (fields.name) cleanFields.name = fields.name.trim();
      cleanFields.strategy = fields.strategy || 'rss';
      if (fields.feed_url?.trim()) cleanFields.feed_url = fields.feed_url.trim();
      if (fields.url?.trim()) cleanFields.url = fields.url.trim();
      if (fields.scrape_parser) cleanFields.scrape_parser = fields.scrape_parser;
      if (fields.description?.trim()) cleanFields.description = fields.description.trim();
      if (fields.feed_hours) cleanFields.feed_hours = Number(fields.feed_hours);
    }

    if (t === 'newsletter') {
      if (fields.name) cleanFields.name = fields.name.trim();
      if (fields.from) cleanFields.from = fields.from.trim();
      cleanFields.lookback_days = Number(fields.lookback_days) || 1;
    }

    if (t === 'github_trending') {
      if (fields.name) cleanFields.name = fields.name.trim();
      if (fields.url) cleanFields.url = fields.url.trim();
      cleanFields.min_stars_week = Number(fields.min_stars_week);
      if (fields.ai_keywords) {
        cleanFields.ai_keywords = fields.ai_keywords
          .split(',')
          .map((k) => k.trim())
          .filter(Boolean);
      }
    }

    return cleanFields;
  }

  async function handleSave() {
    if (!validate()) return;
    setSaving(true);
    setServerError(null);

    try {
      const body = buildBody();
      if (isCreate) {
        const t = activeType;
        let key;
        if (t === 'twitter') key = fields.account.trim();
        else if (t === 'github_trending') key = 'trending';
        else key = manualKey.trim();

        await mutateApi(`/api/digests/${slug}/sources`, 'POST', {
          source_type: t,
          key,
          fields: body,
        });
      } else {
        await mutateApi(
          `/api/digests/${slug}/sources/${sourceType}/${sourceKey}`,
          'PUT',
          { fields: body }
        );
      }
      location.hash = `#/${slug}/sources`;
    } catch (err) {
      setServerError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    setDeleting(true);
    setServerError(null);
    try {
      await mutateApi(
        `/api/digests/${slug}/sources/${sourceType}/${sourceKey}`,
        'DELETE'
      );
      location.hash = `#/${slug}/sources`;
    } catch (err) {
      setServerError(err.message);
      setShowDeleteDialog(false);
    } finally {
      setDeleting(false);
    }
  }

  function renderTextField(name, label, opts = {}) {
    const { required, helper, type = 'text', textarea } = opts;
    return (
      <div class="form-field">
        <md-outlined-text-field
          label={label}
          value={fields[name] || ''}
          onInput={(e) => setField(name, e.target.value)}
          type={textarea ? 'textarea' : type}
          rows={textarea ? 3 : undefined}
          error={!!errors[name]}
          error-text={errors[name] || ''}
          supporting-text={helper || ''}
        />
      </div>
    );
  }

  function renderFields() {
    const t = activeType;

    if (t === 'twitter') {
      return isCreate
        ? renderTextField('account', 'Twitter Handle', {
            required: true,
            helper: 'Without the @ symbol',
          })
        : null;
    }

    if (t === 'blog' || t === 'research') {
      const strategy = fields.strategy || 'rss';
      return (
        <>
          {renderTextField('name', 'Name', { required: true })}
          <div class="form-field">
            <md-outlined-select
              label="Strategy"
              value={strategy}
              onChange={(e) => setField('strategy', e.target.value)}
            >
              <md-select-option value="rss">
                <div slot="headline">RSS</div>
              </md-select-option>
              <md-select-option value="html_scrape">
                <div slot="headline">HTML Scrape</div>
              </md-select-option>
            </md-outlined-select>
          </div>
          {strategy === 'rss' &&
            renderTextField('feed_url', 'Feed URL', { required: true })}
          {strategy === 'html_scrape' && (
            <>
              {renderTextField('url', 'URL', { required: true })}
              <div class="form-field">
                <md-outlined-select
                  label="Scrape Parser"
                  value={fields.scrape_parser || ''}
                  onChange={(e) => setField('scrape_parser', e.target.value)}
                  error={!!errors.scrape_parser}
                  error-text={errors.scrape_parser || ''}
                >
                  {SCRAPE_PARSERS.map((p) => (
                    <md-select-option key={p.value} value={p.value}>
                      <div slot="headline">{p.label}</div>
                    </md-select-option>
                  ))}
                </md-outlined-select>
              </div>
            </>
          )}
          {renderTextField('description', 'Description', { textarea: true })}
          {renderTextField('feed_hours', 'Feed Hours', { type: 'number' })}
        </>
      );
    }

    if (t === 'newsletter') {
      return (
        <>
          {renderTextField('name', 'Name', { required: true })}
          {renderTextField('from', 'From Email', { required: true })}
          <div class="form-field">
            <md-outlined-text-field
              label="Lookback Days"
              value={fields.lookback_days ?? 1}
              onInput={(e) => setField('lookback_days', e.target.value)}
              type="number"
              error={!!errors.lookback_days}
              error-text={errors.lookback_days || ''}
            />
          </div>
        </>
      );
    }

    if (t === 'github_trending') {
      return (
        <>
          {renderTextField('name', 'Name', { required: true })}
          {renderTextField('url', 'URL', { required: true })}
          {renderTextField('min_stars_week', 'Min Stars/Week', {
            required: true,
            type: 'number',
          })}
          {renderTextField('ai_keywords', 'AI Keywords', {
            required: true,
            helper: 'Comma-separated',
          })}
        </>
      );
    }

    return null;
  }

  function renderSharedSettings() {
    return (
      <div class="form-readonly">
        <h3>Shared Settings</h3>
        {Object.entries(sharedSettings).map(([key, val]) => (
          <div class="form-readonly-item" key={key}>
            <span class="form-readonly-label">{key}</span>
            <span>{typeof val === 'object' ? JSON.stringify(val) : String(val)}</span>
          </div>
        ))}
      </div>
    );
  }

  function renderDeleteDialog() {
    return (
      <md-dialog ref={dialogRef} onClose={() => setShowDeleteDialog(false)}>
        <div slot="headline">Delete Source</div>
        <div slot="content">
          Are you sure you want to delete "{fields.name || sourceKey}"? This will
          also remove its tracking state.
        </div>
        <div slot="actions">
          <md-text-button onClick={() => setShowDeleteDialog(false)}>
            Cancel
          </md-text-button>
          <md-text-button class="delete-btn" onClick={handleDelete}>
            {deleting ? 'Deleting...' : 'Delete'}
          </md-text-button>
        </div>
      </md-dialog>
    );
  }

  if (loading) {
    return (
      <div class="loading-container">
        <md-circular-progress indeterminate />
      </div>
    );
  }

  return (
    <div class="source-form">
      <div class="page-header">
        <div style="display: flex; align-items: center; gap: 12px">
          <a href={`#/${slug}/sources`} class="back-link">
            <md-icon>arrow_back</md-icon>
          </a>
          <h1 class="page-title">
            {isCreate ? 'Add Source' : `Edit ${fields.name || sourceKey}`}
          </h1>
        </div>
      </div>

      {serverError && <div class="form-error">{serverError}</div>}

      {isCreate && (
        <div class="form-field">
          <md-outlined-select
            label="Source Type"
            value={selectedType}
            onChange={(e) => {
              setSelectedType(e.target.value);
              setFields({});
              setManualKey('');
              setErrors({});
            }}
          >
            {SOURCE_TYPES.map((t) => (
              <md-select-option
                key={t.value}
                value={t.value}
                disabled={t.value === 'github_trending' && hasGithubTrending}
              >
                <div slot="headline">{t.label}</div>
              </md-select-option>
            ))}
          </md-outlined-select>
        </div>
      )}

      {isCreate && needsManualKey(activeType) && (
        <div class="form-field">
          <md-outlined-text-field
            label="Key"
            value={manualKey}
            onInput={(e) => {
              setManualKey(e.target.value);
              if (errors.key) {
                setErrors((prev) => {
                  const next = { ...prev };
                  delete next.key;
                  return next;
                });
              }
            }}
            error={!!errors.key}
            error-text={errors.key || ''}
            supporting-text="Lowercase alphanumeric and underscores only"
          />
        </div>
      )}

      {renderFields()}

      {sharedSettings &&
        Object.keys(sharedSettings).length > 0 &&
        renderSharedSettings()}

      <div class="form-actions">
        <md-filled-button onClick={handleSave} disabled={saving}>
          {saving ? 'Saving...' : 'Save'}
        </md-filled-button>
        <md-outlined-button
          onClick={() => (location.hash = `#/${slug}/sources`)}
        >
          Cancel
        </md-outlined-button>
        {!isCreate && (
          <md-text-button
            class="delete-btn"
            onClick={() => setShowDeleteDialog(true)}
          >
            Delete
          </md-text-button>
        )}
      </div>

      {!isCreate && renderDeleteDialog()}
    </div>
  );
}
