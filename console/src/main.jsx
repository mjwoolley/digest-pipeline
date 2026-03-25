import { render } from 'preact';
import { App } from './app';

// Material Web component imports
import '@material/web/button/filled-button.js';
import '@material/web/button/outlined-button.js';
import '@material/web/button/text-button.js';
import '@material/web/chips/assist-chip.js';
import '@material/web/chips/chip-set.js';
import '@material/web/divider/divider.js';
import '@material/web/icon/icon.js';
import '@material/web/iconbutton/icon-button.js';
import '@material/web/list/list.js';
import '@material/web/list/list-item.js';
import '@material/web/progress/circular-progress.js';
import '@material/web/tabs/tabs.js';
import '@material/web/tabs/primary-tab.js';
import '@material/web/switch/switch.js';

import './styles.css';

render(<App />, document.getElementById('app'));
