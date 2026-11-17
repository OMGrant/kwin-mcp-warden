// The drop shadow under the consent card and the corner notice: Qt Quick's
// RectangularShadow, which Qt has from 6.9 on. card.qml and overlay.qml load it through a
// Loader, so on an older Qt (Debian 13 ships 6.8) the window still opens, without the
// shadow, instead of failing to load and leaving every request unanswered.
import QtQuick
import QtQuick.Effects

RectangularShadow {}
