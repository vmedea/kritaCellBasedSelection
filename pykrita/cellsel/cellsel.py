# Krita grid-based selection plugin.
# Mara Huldra 2022
# SPDX-License-Identifier: MIT

from krita import (
        Krita,
        Selection,
        Extension)

from PyQt5.QtCore import (
        Qt,
        QObject,
        QEvent,
        QPointF)

from PyQt5.QtGui import (
        QTransform,
        QInputEvent)

from PyQt5.QtWidgets import (
        QWidget,
        QMdiArea,
        QAbstractScrollArea,
        QSizePolicy,
        QSpinBox)


# These functions are from: https://krita-artists.org/t/hot-to-get-the-mouse-position-in-a-plugin/41012/ by AkiR
def get_q_view(view):
    window = view.window()
    q_window = window.qwindow()
    q_stacked_widget = q_window.centralWidget()
    q_mdi_area = q_stacked_widget.findChild(QMdiArea)
    for v, q_mdi_view in zip(window.views(), q_mdi_area.subWindowList()):
        if v == view:
            return q_mdi_view.widget()


def get_q_canvas(q_view):
    scroll_area = q_view.findChild(QAbstractScrollArea)
    viewport = scroll_area.viewport()
    for child in viewport.children():
        cls_name = child.metaObject().className()
        if cls_name.startswith('Kis') and ('Canvas' in cls_name):
            return child


def get_transform(view):
    def _offset(scroller):
        mid = (scroller.minimum() + scroller.maximum()) / 2.0
        return -(scroller.value() - mid)
    canvas = view.canvas()
    document = view.document()
    q_view = get_q_view(view)
    area = q_view.findChild(QAbstractScrollArea)
    zoom = (canvas.zoomLevel() * 72.0) / document.resolution()
    transform = QTransform()
    transform.translate(
            _offset(area.horizontalScrollBar()),
            _offset(area.verticalScrollBar()))
    transform.rotate(canvas.rotation())
    transform.scale(zoom, zoom)
    return transform

# Use configured grid cell size
def get_grid_settings(krita):
    '''
    There's no way to get at the grid settings directly, as far as I know.
    Luckily, we can dig into the docker and extract values directly.
    '''
    for docker in krita.dockers():
        if docker.metaObject().className() == "GridDockerDock":
            widget = docker.widget()
            hspacing = widget.findChild(QSpinBox, "intHSpacing")
            vspacing = widget.findChild(QSpinBox, "intVSpacing")
            if hspacing is not None and vspacing is not None:
                return (hspacing.value(), vspacing.value())

    return (32, 32) # Sane default

class MouseInterceptor(QWidget):
    def __init__(self, parent, view, document, cell_size):
        super().__init__(parent)
        self.view = view
        self.document = document

        (self.cell_w, self.cell_h) = cell_size
        
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setGeometry(0, 0, parent.width(), parent.height())
        self.setMouseTracking(True)
        
        self.cur_cell = None
        self.cur_cell_value = 0
        
    def pos_to_grid(self, local_pos):
        '''Convert local position to document coordinates.'''
        transform = get_transform(self.view)
        transform_inv, _ = transform.inverted()
        center = self.rect().center()
        document_center = QPointF(0.5 * self.document.width(), 0.5 * self.document.height())
        pos = transform_inv.map(local_pos - QPointF(center)) + QPointF(document_center)
        
        xpos, ypos = int(pos.x()), int(pos.y())
        if xpos < 0 or ypos < 0 or xpos >  self.document.width() or ypos > self.document.height():
            # Out of bounds, nothing to do.
            return None
        
        return (xpos // self.cell_w, ypos // self.cell_h)
        
    def set_cell(self, cell, newval):
        '''
        Select, unselect or toggle a cell.
        '''
        (cell_x, cell_y) = cell
        sel = self.document.selection()
        if sel is None:
            sel = Selection()
        
        if newval is None: # Get current value, for toggle.
            d = sel.pixelData(cell_x * self.cell_w, cell_y * self.cell_h, 1, 1)
            if d[0][0]:
                newval = 0
            else:
                newval = 255
        
        sel.select(cell_x * self.cell_w, cell_y * self.cell_h, self.cell_w, self.cell_h, newval)
        
        self.document.setSelection(sel)    
        return newval
        
    def input_press(self, pos, mods):
        '''
        Button is pressed.
        '''
        # XXX mods ShiftModifier ControlModifier AltModifier could affect selection.
        cell = self.pos_to_grid(pos)
        if cell is None:
            return
        
        self.cur_cell_value = self.set_cell(cell, None)
        self.cur_cell = cell
        
    def input_release(self, pos, mods):
        '''
        Button is released.
        '''
        self.cur_cell = None

    def input_move(self, pos, mods):
        '''
        Allow "drawing" by keeping the button pressed.
        '''
        if self.cur_cell is None:
            return
        cell = self.pos_to_grid(pos)
        if cell is None or cell == self.cur_cell:
            return
        
        self.set_cell(cell, self.cur_cell_value)
        self.cur_cell = cell

    def event(self, event):
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            #print('Mouse press event')
            self.input_press(event.localPos(), event.modifiers())
            event.accept()
            return True
        if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            #print('Mouse release event')
            self.input_release(event.localPos(), event.modifiers())
            event.accept()
            return True
        if event.type() == QEvent.MouseMove:
            #print('Mouse move event')
            self.input_move(event.localPos(), event.modifiers())
            return False # Parent should see this, otherwise there's no feedback for position.
        if event.type() == QEvent.TabletPress and event.button() == Qt.LeftButton:
            #print('Tablet press event')
            self.input_press(event.pos(), event.modifiers())
            event.accept()
            return True
        if event.type() == QEvent.TabletRelease and event.button() == Qt.LeftButton:
            #print('Tablet release event')
            self.input_release(event.pos(), event.modifiers())
            event.accept()
            return True
        if event.type() == QEvent.TabletMove:
            #print('Tablet move event')
            self.input_move(event.pos(), event.modifiers())
            return True # Parent should see this, otherwise there's no feedback for position.
        
        return super().event(event)
    

class KeyFilter(QObject):
    '''
    Key filter to detect action key release.
    '''
    def __init__(self, action, q_window, q_canvas, view, document, cell_size, parent=None):
        super().__init__(parent)
        self.action = action
        self.q_window = q_window
        self.q_canvas = q_canvas
        self.view = view
        self.document = document
        self.cell_size = cell_size

    def eventFilter(self, obj, e):
        if e.type() == QEvent.KeyRelease:
            if self.action.shortcut().matches(e.key()) > 0 and not e.isAutoRepeat():
	            self.deactivate()

        return False
	    
    def activate(self):
        '''
        Activate special selection mode.
        '''
        print('Activate')
        # Install mouse interceptor.
        self.i = MouseInterceptor(self.q_canvas, self.view, self.document, self.cell_size)
        self.i.show()
        
    def deactivate(self):
        '''
        Deactivate special selection mode.
        '''
        print('Deactivate')
        
        # Remove the mouse interceptor widget.
        self.i.deleteLater()

        # Remove the event filter from the canvas too.        
        self.q_window.removeEventFilter(self)


class MyExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent)

    def setup(self):
        pass
        
    def createActions(self, window):
        self.action = window.createAction("cellsel", "Cell-based selection", "")
        self.action.setAutoRepeat(False)
        self.action.triggered.connect(self.handleAction)
        
    def handleAction(self):
        app = Krita.instance()
        view = app.activeWindow().activeView()
        document = view.document()
        q_view = get_q_view(view)
        q_canvas = get_q_canvas(q_view)

        window = app.activeWindow()
        q_window = window.qwindow()

        cell_size = get_grid_settings(app)

        self.fil = KeyFilter(self.action, q_window, q_canvas, view, document, cell_size)
        self.fil.activate()
        q_window.installEventFilter(self.fil)


Krita.instance().addExtension(MyExtension(Krita.instance()))

